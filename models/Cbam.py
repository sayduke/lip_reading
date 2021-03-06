import torch
import math
import torch.nn as nn
import torch.nn.functional as F

class BasicConv(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, stride=1, padding=0, dilation=1, groups=1, relu=True, bn=True, bias=False):
        super(BasicConv, self).__init__()
        self.out_channels = out_planes
        self.conv = nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation, groups=groups, bias=bias)
        self.bn = nn.BatchNorm2d(out_planes,eps=1e-5, momentum=0.01, affine=True) if bn else None
        self.relu = nn.ReLU() if relu else None

    def forward(self, x):
        x = self.conv(x)
        if self.bn is not None:
            x = self.bn(x)
        if self.relu is not None:
            x = self.relu(x)
        return x

class Flatten(nn.Module):
    def forward(self, x):
        return x.view(x.size(0), -1)

class ChannelGate(nn.Module):
    def __init__(self, gate_channels, reduction_ratio=16, pool_types=['avg', 'max'], dropout=0.2):
        super(ChannelGate, self).__init__()
        self.gate_channels = gate_channels
        self.mlp = nn.Sequential(
            Flatten(),
            nn.Linear(gate_channels, gate_channels // reduction_ratio),
            nn.ReLU(),
            nn.Linear(gate_channels // reduction_ratio, gate_channels)
            )
        self.pool_types = pool_types
        self.dropout = nn.Dropout2d(dropout) if dropout > 0. else None
    def forward(self, x, landmark):
        if isinstance(landmark, bool):
            landmark = x
        channel_att_sum = None
        for pool_type in self.pool_types:
            if pool_type=='avg':
                avg_pool = F.avg_pool2d(landmark, (landmark.size(2), landmark.size(3)))
                channel_att_raw = self.mlp(avg_pool)
            elif pool_type=='max':
                max_pool = F.max_pool2d(landmark, (landmark.size(2), landmark.size(3)))
                channel_att_raw = self.mlp(max_pool)
            elif pool_type=='lp':
                lp_pool = F.lp_pool2d(landmark, 2, (landmark.size(2), landmark.size(3)))
                channel_att_raw = self.mlp(lp_pool)
            elif pool_type=='lse':
                # LSE pool only
                lse_pool = logsumexp_2d(x)
                channel_att_raw = self.mlp( lse_pool )

            if channel_att_sum is None:
                channel_att_sum = channel_att_raw
            else:
                channel_att_sum = channel_att_sum + channel_att_raw

        scale = torch.sigmoid(channel_att_sum).unsqueeze(2).unsqueeze(3).expand_as(x)
        if self.dropout:
            scale = self.dropout(scale)
        return x * scale

def logsumexp_2d(tensor):
    tensor_flatten = tensor.view(tensor.size(0), tensor.size(1), -1)
    s, _ = torch.max(tensor_flatten, dim=2, keepdim=True)
    outputs = s + (tensor_flatten - s).exp().sum(dim=2, keepdim=True).log()
    return outputs

class ChannelPool(nn.Module):
    def forward(self, x):
        return torch.cat((torch.max(x,1)[0].unsqueeze(1), torch.mean(x,1).unsqueeze(1)), dim=1)

class SpatialGate(nn.Module):
    def __init__(self, dropout=0.2):
        super(SpatialGate, self).__init__()
        kernel_size = 7
        self.dropout = nn.Dropout2d(dropout) if dropout > 0. else None
        self.compress = ChannelPool()
        self.spatial = BasicConv(2, 1, kernel_size, stride=1, padding=(kernel_size-1) // 2, relu=False)
    
    def forward(self, x, landmark):
        if isinstance(landmark, bool):
            landmark = x
        x_compress = self.compress(landmark)
        x_out = self.spatial(x_compress)
        scale = torch.sigmoid(x_out) # broadcasting
        if self.dropout:
            scale = self.dropout(scale)
        return x * scale

class TemporalGate(nn.Module):
    def __init__(self, gate_temporal=29, linear_size=5, pool_types=['avg', 'max'], dropout=0.2):
        super(TemporalGate, self).__init__()
        self.dropout = nn.Dropout2d(0.2)
        self.gate_channels = gate_temporal
        self.mlp = nn.Sequential(
            Flatten(),
            nn.Linear(gate_temporal, linear_size),
            nn.ReLU(),
            nn.Linear(linear_size, gate_temporal)
            )
        self.pool_types = pool_types

    def forward(self, x, landmark):
        if isinstance(landmark, bool):
            landmark = x
        bs, c, h, w = x.size()
        x = x.view(int(bs/29), 29, c, h, w)
        landmark = landmark.view(int(bs/29), 29, -1)
        temporal_att_sum = None
        for pool_type in self.pool_types:
            if pool_type=='avg':
                avg_pool = F.avg_pool1d(landmark, kernel_size=landmark.size(2))
                temporal_att_sum = self.mlp(avg_pool)
            elif pool_type=='max':
                max_pool = F.max_pool1d(landmark, kernel_size=landmark.size(2))
                temporal_att_sum = self.mlp(max_pool)

            if temporal_att_sum is None:
                temporal_att_sum = temporal_att_sum
            else:
                temporal_att_sum = temporal_att_sum + temporal_att_sum
        #bs, 29, 1, 1, 1
        scale = torch.sigmoid(temporal_att_sum).unsqueeze(2).unsqueeze(3).unsqueeze(4).expand_as(x)
        return (x * scale).view(-1, c, h, w)


class CBAM(nn.Module):
    def __init__(self, channel, in_channel, stride, kernel_size=3, padding=1, reduction_ratio=16, pool_types=['avg', 'max'], no_channel=False, no_spatial=False, no_temporal=True, dropout=0.2):
        super(CBAM, self).__init__()
        self.ChannelGate = ChannelGate(channel, reduction_ratio, pool_types, dropout=dropout if no_spatial and no_temporal else 0.)
        self.no_spatial = no_spatial
        self.no_temporal = no_temporal
        self.no_channel = no_channel
        self.resize = nn.Sequential(
            nn.Conv2d(in_channel, channel, kernel_size=kernel_size, stride=stride, padding=padding, bias=False),
            nn.BatchNorm2d(channel),
        )
        if not no_spatial:
            self.SpatialGate = SpatialGate(dropout=dropout if not no_temporal else 0.)
        if not no_temporal:
            self.temporalGate = TemporalGate(dropout=dropout)
            
    def forward(self, x, landmark=False):
        if not isinstance(landmark, bool):
            landmark = self.resize(landmark)
        if not self.no_channel:
            x = self.ChannelGate(x, landmark)
        if not isinstance(landmark, bool):
            landmark = F.relu(landmark)
        if not self.no_spatial:
            x = self.SpatialGate(x, landmark)
        if not self.no_temporal:
            x = self.temporalGate(x, landmark)
        return x, landmark