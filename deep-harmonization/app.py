from flask import Flask, request, Response
import sys
import time
import copy
import traceback

import base64
import json
import io

import numpy as np
from imageio import imread
from scipy.ndimage.filters import gaussian_filter
from PIL import Image

import torch
from torch import nn
import torch.nn.functional as F
from torch.nn import MSELoss
from torch.optim import Adam, LBFGS
from torchvision.transforms import ToTensor, ToPILImage, Normalize, Compose
from torchvision.models import vgg19
from torchvision.utils import save_image

from models import gram_matrix, patch_match, downsampling, cosine_similarity
from data_utils import read_img

app = Flask(__name__)

class ContentLoss(nn.Module):

    def __init__(self, target, mask):
        super(ContentLoss, self).__init__()
        # we 'detach' the target content from the tree used
        # to dynamically compute the gradient: this is a stated value,
        # not a variable. Otherwise the forward method of the criterion
        # will throw an error.
        self.mask = mask
        self.target = target.detach()

    def forward(self, x):
        self.loss = F.mse_loss(x * self.mask, self.target * self.mask)
        return x

class StyleLoss(nn.Module):

    def __init__(self, target_feature, mask):
        super(StyleLoss, self).__init__()
        self.mask = mask
        self.target = (gram_matrix(target_feature * self.mask).float() / self.mask.sum()).detach()

    def forward(self, x):
        G = gram_matrix(x * self.mask).float() / self.mask.sum()
        self.loss = F.mse_loss(G, self.target)
        return x

normalization_mean = torch.tensor([103.939, 116.779, 123.68]).cuda()
normalization_std = torch.tensor([1.0, 1.0, 1.0]).cuda()

# create a module to normalize input image so we can easily put it in a
# nn.Sequential
class Normalization(nn.Module):
    def __init__(self, mean, std):
        super(Normalization, self).__init__()
        # .view the mean and std to make them [C x 1 x 1] so that they can
        # directly work with image Tensor of shape [B x C x H x W].
        # B is batch size. C is number of channels. H is height and W is width.
        self.mean = torch.tensor(mean).view(-1, 1, 1)
        self.std = torch.tensor(std).view(-1, 1, 1)

    def forward(self, img):
        # normalize img
        return (img * 255.0 - self.mean) / self.std


# cnn = vgg19()
# cnn.load_state_dict(torch.load('VGG_ILSVRC_19_layers.pytorch'))
cnn = torch.hub.load('pytorch/vision:v0.6.0', 'vgg19', pretrained=True)
# cnn.eval()
cnn = cnn.features.cuda().eval()



def get_style_model_and_losses(cnn, normalization, style_img, content_img, mask_img, content_layers = ['relu4_1'],
                               style_layers = ['relu3_1', 'relu4_1', 'relu5_1']):

    cnn = copy.deepcopy(cnn)
    normalization = copy.deepcopy(normalization)

    mask = mask_img.unsqueeze(0)
    content_losses = []
    style_losses = []

    # assuming that cnn is a nn.Sequential, so we make a new nn.Sequential
    # to put in modules that are supposed to be activated sequentially
    model = nn.Sequential(normalization)

    i = 1 # increment every time we see a pool
    j = 0 # increment every time we see a conv, reset to 0 every time we see a pool
    for layer in cnn.children():
        if isinstance(layer, nn.Conv2d):
            j += 1
            name = 'conv{}_{}'.format(i, j)
            mask = F.avg_pool2d(mask, 3, stride = 1, padding = 1)
        elif isinstance(layer, nn.ReLU):
            name = 'relu{}_{}'.format(i, j)
            # The in-place version doesn't play very nicely with the ContentLoss
            # and StyleLoss we insert below. So we replace with out-of-place
            # ones here.
            layer = nn.ReLU(inplace = False)
        elif isinstance(layer, nn.MaxPool2d):
            name = 'pool{}_{}'.format(i, j)
            mask = downsampling(mask, scale_factor = 0.5)
            i += 1
            j = 0
        elif isinstance(layer, nn.BatchNorm2d):
            name = 'bn{}_{}'.format(i, j)
        else:
            raise RuntimeError('Unrecognized layer: {}'.format(layer.__class__.__name__))

        print("Adding " + name)
        model.add_module(name, layer)

        if name in content_layers:
            # add content loss:
            target = model(content_img).detach()
            content_loss = ContentLoss(target, mask.expand_as(target))

            model.add_module("content_loss{}_{}".format(i, j), content_loss)
            content_losses.append(content_loss)

            print("Adding content loss at " + name)

        if name in style_layers:
            # add style loss:
            target_feature = model(style_img)
            target_content = model(content_img).detach()
            target_match = patch_match(target_content, target_feature, patch_size = 3).detach()
            style_loss = StyleLoss(target_match, mask.expand_as(target_match))

            model.add_module("style_loss{}_{}".format(i, j), style_loss)
            style_losses.append(style_loss)

            print('no matching: ', torch.mean(cosine_similarity(target_content, target_feature)))
            print('matched: ', torch.mean(cosine_similarity(target_content, target_match)))
            print("Adding style loss at " + name)

    # now we trim off the layers after the last content and style losses
    for i in range(len(model) - 1, -1, -1):
        if isinstance(model[i], ContentLoss) or isinstance(model[i], StyleLoss):
            break

    model = model[:(i + 1)]

    return model, style_losses, content_losses

 
# input_img = content_img.clone()
# if you want to use a white noise instead uncomment the below line:
# input_img = torch.randn(content_img.data.size(), device=device)

# add the original input image to the figure:
# plt.figure()
# imshow(input_img, title='Input Image')

 
def run_style_transfer(cnn, normalization, content_img, style_img, input_img, mask_img, num_steps = 500,
                       style_weight = 100, content_weight = 5):
    """Run the style transfer."""
    print('Building the style transfer model..')
    model, style_losses, content_losses = get_style_model_and_losses(cnn, normalization, style_img, content_img, mask_img)
    optimizer = LBFGS([input_img.requires_grad_()], max_iter=num_steps,lr = 1)

    print('Optimizing..')
    run = [0]
    def closure():
        optimizer.zero_grad()
        model(input_img)
        style_score = 0
        content_score = 0

        for sl in style_losses:
            style_score += sl.loss
        for cl in content_losses:
            content_score += cl.loss

        style_score *= style_weight
        content_score *= content_weight

        loss = style_score + content_score
        loss.backward()

        if run[0] % 100 == 0:
            print("run {}:".format(run))
            print('Style Loss : {} Content Loss: {}'.format(style_score.item(), content_score.item()))
            # print()
            # plt.figure(figsize = (8, 8))
            #imshow(input_img.clone())
        run[0] += 1

        return style_score + content_score

    optimizer.step(closure)

    # a last correction...
    input_img.data.clamp_(0, 1)

    return input_img


def image_loader(input_):
    image_data = base64.b64decode(input_)
    image = Image.open(io.BytesIO(image_data)).convert("RGB")
    # fake batch dimension required to fit network's input dimensions
    image = loader(image).unsqueeze(0)
    return image.cuda()

loader = ToTensor()
unloader = ToPILImage()

@app.route("/", methods=["POST", "OPTIONS"])
def index():
    res = Response(headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type, X-Requested-With",
        "Access-Control-Allow-Methods": "POST, OPTIONS"
    })

    try:
        req = request.get_json()
        if type(req) is dict:
            style = req['style']
            content = req['content']
            mask = req['mask']
        else:
            res.data = "Invalid Request"
            return res
        
        key = "base64,"

        index = style.find(key)
        if(index != -1):
            style = style[index+len(key):]

        index = content.find(key)
        if(index != -1):
            content = content[index+len(key):]

        index = mask.find(key)
        if(index != -1):
            mask = mask[index+len(key):]


        style_img = image_loader(style)
        content_img = image_loader(content)
        mask_img = imread(base64.b64decode(mask)).astype(np.float32)
        if mask_img.shape[-1] == 3:
            mask_img = mask_img[..., 0]
        tmask_img = mask_img
        tmask_img = gaussian_filter(tmask_img, sigma = 3)
        tmask_img = torch.from_numpy(tmask_img).unsqueeze(0).cuda() / 255.0
        mask_img = torch.from_numpy(mask_img).unsqueeze(0).cuda() / 255.0
        input_img = content_img.clone()
        output = run_style_transfer(cnn, Normalization(normalization_mean, normalization_std),
                            content_img, style_img, input_img, mask_img)
        output = tmask_img * output + (1 - tmask_img) * style_img

        # output = Image.fromarray(output)
        output = unloader(output[0].cpu())

        buffer = io.BytesIO()
        output.save(buffer, format="PNG")
        response = base64.b64encode(buffer.getvalue())
        res.mimetype = "application/json"
        res.data = json.dumps({"res": "data:image/png;base64," + str(response)[2:-1]})
        return res
    except Exception as e:
        res.data = traceback.format_exc()
        return res
        

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True)