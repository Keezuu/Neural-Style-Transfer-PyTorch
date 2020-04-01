import torchvision
from time import strftime, gmtime

import torch.nn as nn
import torchvision.models as models

import copy
import torch.optim as optim
from torch.backends import cudnn
from torch.utils.tensorboard import SummaryWriter

from Layers.NormalizeLayer import NormalizeLayer
from Layers.AdainStyleLayer import AdainStyleLayer

from resources.utilities import *

import torch.nn.functional as F

cudnn.benchmark = True
cudnn.enabled = True

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
normalize_mean = [0.485, 0.456, 0.406]
normalize_std = [0.229, 0.224, 0.225]


class AdaIN(object):

    def __init__(self, depth, style_req):
        self.style_layers = []
        self.encoder = self.build_encoder(depth, style_req)
        self.decoder = self.build_decoder(depth)

    def build_encoder(self, depth, style_req):
        """Builds an encoder that uses first "depth" numbers of convolutional layers of VGG19"""
        vgg = models.vgg19(pretrained=True).features.to(device).eval()
        # Define the layer names from which we want to pick activations

        # Create a new model that will be modified version of given model
        # starts with normalization layer to ensure all images that are
        # inserted are normalized like the ones original model was trained on
        norm_layer = NormalizeLayer(normalize_mean, normalize_std)

        model = nn.Sequential(norm_layer)
        model = model.to(device)

        i = 0
        # Loop over the layers
        for layer in vgg.children():
            # The layers in vgg are not numerated so we have to add numeration
            # to copied layers so we can append our content and style layers to it
            name = ""
            # Check which instance this layer is to name it appropiately
            if isinstance(layer, nn.Conv2d):
                i += 1
                # Stop when we reach required depth
                if i > depth:
                    break
                name = "Conv2d_{}".format(i)
            if isinstance(layer, nn.ReLU):
                name = "ReLu_{}".format(i)
                layer = nn.ReLU(inplace=False)
            if isinstance(layer, nn.MaxPool2d):
                if i >= depth:
                    break
                name = "MaxPool2d_{}".format(i)
            # Layer has now numerated name so we can find it easily
            # Add it to our model
            model.add_module(name, layer)

            # Check for style layers
            if name in style_req:
                # Create the style layer
                style_layer = AdainStyleLayer()
                # Append it to the module
                model.add_module("StyleLayer_{}".format(i), style_layer)
                self.style_layers.append(style_layer)

        return model.eval()

    def build_decoder(self, depth):
        """Decoder mirrors the encoder architecture"""
        # TODO: FOR NOW WE ASSUME DEPTH = 4

        model = nn.Sequential()

        # Build decoder for depth = 4
        model.add_module("ConvTranspose2d_1", nn.ConvTranspose2d(128, 128, (3, 3), (1, 1), (1, 1)))
        model.add_module("ReLU_1", nn.ReLU())

        model.add_module("ConvTranspose2d_2", nn.ConvTranspose2d(128, 64, (3, 3), (1, 1), (1, 1)))
        model.add_module("ReLU_2", nn.ReLU())
        model.add_module("Upsample_2", nn.Upsample(scale_factor=2))

        model.add_module("ConvTranspose2d_3", nn.ConvTranspose2d(64, 64, (3, 3), (1, 1), (1, 1)))
        model.add_module("ReLU_3", nn.ReLU())

        model.add_module("ConvTranspose2d_4", nn.ConvTranspose2d(64, 3, (3, 3), (1, 1), (1, 1)))
        model.add_module("ReLU_4", nn.ReLU())

        # Send model to CUDA or CPU
        return model.train().to(device)

    def adain(self, style_features, content_features):
        """Based on section 5. of https://arxiv.org/pdf/1703.06868.pdf"""
        with torch.no_grad():
            # Pytorch shape - NxCxHxW
            # Computing values across spatial dimensions
            # Compute std of content_features
            content_std = torch.std(content_features, [2, 3], keepdim=True)
            # Compute mean of content_features
            content_mean = torch.mean(content_features, [2, 3], keepdim=True)
            # Compute std of style_features
            style_std = torch.std(style_features, [2, 3], keepdim=True)
            # Compute mean of style_features
            style_mean = torch.mean(style_features, [2, 3], keepdim=True)

            return style_std * ((content_features - content_mean)/content_std) + style_mean

    def forward(self, style_image, content_image, alpha=1.0):
        with torch.no_grad():
            # Encode style and content image
            style_features = self.encoder(style_image)
            content_features = self.encoder(content_image)
            # Compute AdaIN
            adain_result = self.adain(style_features, content_features)
            adain_result = alpha * adain_result + (1 - alpha) * content_features

        # Decode to image
        generated_image = self.decoder(adain_result)

        # return image and adain result
        return generated_image, adain_result

    def compute_style_loss(self, style, generated):
        # Compute std and mean of input
        style_std = torch.std(style, [2, 3], keepdim=True)
        style_mean = torch.mean(style, [2, 3], keepdim=True)
        # Compute std and mean of target
        generated_std = torch.std(generated, [2, 3], keepdim=True)
        generated_mean = torch.mean(generated, [2, 3], keepdim=True)
        return F.mse_loss(style_mean, generated_mean) + \
            F.mse_loss(style_std, generated_std)

    def compute_loss(self, generated_image, style_image, adain_result):
        style_activations = []
        decoded_activations = []

        # Get the style image activations from network
        with torch.no_grad():
            self.encoder(style_image)
            for sl in self.style_layers:
                style_activations.append(sl.activations)

        # Get the decoded image activations from network
        gen_features = self.encoder(generated_image)
        for sl in self.style_layers:
            decoded_activations.append(sl.activations)

        # Compute the cumulative value of style loss
        style_loss = 0
        for sa, da in zip(style_activations, decoded_activations):
            style_loss += self.compute_style_loss(sa, da)

        # Content loss, L2 norm
        content_loss = torch.dist(adain_result, gen_features)

        return style_loss, content_loss

    def train(self, dataloader, style_weight, epochs):

        def adjust_learning_rate(optimizer, lr, lr_decay, iteration_count):
            """Imitating the original implementation"""
            new_lr = lr / (1.0 + lr_decay * iteration_count)
            for param_group in optimizer.param_groups:
                param_group['lr'] = new_lr

        opt = optim.Adam(self.decoder.parameters(), lr=1e-4)

        style_losses = []
        content_losses = []

        # TensorBoard visualization
        writer = SummaryWriter()

        sample = next(iter(dataloader))
        content_image_test = sample['content'].to(device)
        style_image_test = sample['style'].to(device)
        show_tensor(content_image_test, "content", 1)
        show_tensor(style_image_test, "style", 1)

        # Logs for tensorboard
        grid = torchvision.utils.make_grid(style_image_test)
        grid2 = torchvision.utils.make_grid(content_image_test)

        writer.add_image('style_image', grid, 0)
        writer.add_image('content_image', grid2, 0)

        style_feat = self.encoder(style_image_test)
        content_feat = self.encoder(content_image_test)

        adain = self.adain(style_feat, content_feat)


        writer.add_graph(self.encoder, style_image_test)
       # writer.add_graph(self.encoder, content_image_test)
       # writer.add_graph(self.decoder, adain)

        writer.flush()
        writer.close()

        for epoch in range(epochs):
            adjust_learning_rate(opt, 1e-4, 5e-5, epoch)
            for i_batch, sample in enumerate(dataloader):

                content_image = sample['content'].to(device)
                style_image = sample['style'].to(device)

                opt.zero_grad()

                gen_image, adain = self.forward(style_image, content_image)
                style_loss, content_loss = self.compute_loss(gen_image, style_image, adain)
                style_loss = style_loss*style_weight
                total_loss = style_loss + content_loss

                total_loss.backward()

                opt.step()

                # Check network performance every x steps
                if i_batch == 0:
                    with torch.no_grad():
                        test, _ = self.forward(style_image_test, content_image_test)
                        show_tensor(test, epoch, 1)
                    print("Epoch {0} at {1}:".format(epoch, strftime("%Y-%m-%d %H:%M:%S", gmtime())))
                    print('Style Loss(w/ style weight) : {:4f} Content Loss: {:4f}'.format(
                        style_loss.item(), content_loss.item()))
                    print()

                    # Plot the loss
                    style_losses.append(style_loss.item())
                    content_losses.append(content_loss.item())
                    plt.figure()
                    plt.plot(range(epoch+1), style_losses, label="style loss")
                    plt.plot(range(epoch+1), content_losses, label="content loss")
                    plt.legend()
                    plt.savefig('loss.png')
                    plt.close()

        # Save decoder after training
        torch.save(self.decoder.state_dict(), "decoder.pth")

    def load_save(self):
        self.decoder.load_state_dict(torch.load("decoder.pth"))
