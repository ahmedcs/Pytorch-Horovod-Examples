from __future__ import print_function

import torch
import torch.nn as nn
import torch.nn.init as init
import torch.optim as optim
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
import config as cf

import torchvision
import torchvision.transforms as transforms
import torchvision.datasets as datasets

import os
import sys
import time
import argparse
import datetime

from torch.autograd import Variable
import numpy as np
from preresnet import *
import torch.utils.data.distributed
import horovod.torch as hvd

def conv3x3(in_planes, out_planes, stride=1):
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride, padding=1, bias=True)

def conv_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        init.xavier_uniform(m.weight, gain=np.sqrt(2))
        if not m.bias is None:
            init.constant(m.bias, 0)
    elif classname.find('BatchNorm') != -1:
        init.constant(m.weight, 1)
        if not m.bias is None:
            init.constant(m.bias, 0)

class wide_basic(nn.Module):
    def __init__(self, in_planes, planes, dropout_rate, stride=1):
        super(wide_basic, self).__init__()
        self.bn1 = nn.BatchNorm2d(in_planes)
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=3, padding=1, bias=True)
        self.dropout = nn.Dropout(p=dropout_rate)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride, padding=1, bias=True)

        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, planes, kernel_size=1, stride=stride, bias=True),
            )

    def forward(self, x):
        out = self.dropout(self.conv1(F.relu(self.bn1(x))))
        out = self.conv2(F.relu(self.bn2(out)))
        out += self.shortcut(x)

        return out

class Wide_ResNet(nn.Module):
    def __init__(self, depth, widen_factor, dropout_rate, num_classes):
        super(Wide_ResNet, self).__init__()
        self.in_planes = 16

        assert ((depth-4)%6 ==0), 'Wide-resnet depth should be 6n+4'
        n = (depth-4)/6
        k = widen_factor

        print('| Wide-Resnet %dx%d' %(depth, k))
        nStages = [16, 16*k, 32*k, 64*k]

        self.conv1 = conv3x3(3,nStages[0])
        self.layer1 = self._wide_layer(wide_basic, nStages[1], n, dropout_rate, stride=1)
        self.layer2 = self._wide_layer(wide_basic, nStages[2], n, dropout_rate, stride=2)
        self.layer3 = self._wide_layer(wide_basic, nStages[3], n, dropout_rate, stride=2)
        self.bn1 = nn.BatchNorm2d(nStages[3], momentum=0.9)
        self.linear = nn.Linear(nStages[3], num_classes)

    def _wide_layer(self, block, planes, num_blocks, dropout_rate, stride):
        strides = [stride] + [1]*int(num_blocks-1)
        layers = []

        for stride in strides:
            layers.append(block(self.in_planes, planes, dropout_rate, stride))
            self.in_planes = planes

        return nn.Sequential(*layers)

    def forward(self, x):
        out = self.conv1(x)
        out = self.layer1(out)
        out = self.layer2(out)
        out = self.layer3(out)
        out = F.relu(self.bn1(out))
        out = F.avg_pool2d(out, 8)
        out = out.view(out.size(0), -1)
        out = self.linear(out)

        return out

parser = argparse.ArgumentParser(description='PyTorch CIFAR-100 Training')
parser.add_argument('--datadir', required=True, type=str, help='data directory')
parser.add_argument('--lr', default=1./(2**12), type=float, help='learning_rate')
parser.add_argument('--depth', default=28, type=int, help='depth of model')
parser.add_argument('--widen_factor', default=10, type=int, help='width of model')
parser.add_argument('--batch-size', default=128, type=int, help='width of model')
parser.add_argument('--warmup-epoch', default=20, type=int, help='lr warmup')
parser.add_argument('--dataset', default='CIFAR100', type=str, help='dropout_rate')
parser.add_argument('--arch', default='WIDERESNET', type=str, help='dropout_rate')
parser.add_argument('--dropout', default=0.3, type=float, help='dropout_rate')
parser.add_argument('--resume', '-r', action='store_true', help='resume from checkpoint')
parser.add_argument('--testOnly', '-t', action='store_true', help='Test mode with the saved model')
parser.add_argument('--multi-gpu', action='store_true', help='Test mode with the saved model')
args = parser.parse_args()

# Hyper Parameter settings
'''
1. initialize Horovod
'''
hvd.init()

print ("use cuda!!")
print ("local rank {}, rank {}".format(hvd.local_rank(),hvd.rank()))
torch.cuda.set_device(hvd.local_rank())
torch.cuda.manual_seed(1111)

best_acc = 0
start_epoch, num_epochs, batch_size, optim_type = cf.start_epoch, cf.num_epochs, args.batch_size, cf.optim_type
print ("device count {}".format(torch.cuda.device_count()))
print ("batch size {} per node".format(batch_size))
if args.multi_gpu:
    batch_size = batch_size * torch.cuda.device_count()
    print ("batch size {} in total".format(batch_size))
if args.dataset=='CIFAR100':
    # Data Uplaod
    print('\n[Phase 1] : Data Preparation')
    transform_train = transforms.Compose([
    transforms.RandomCrop(32, padding=4),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
    transforms.Normalize(cf.mean['cifar100'], cf.std['cifar100']),
    ]) # meanstd transformation

    transform_test = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize(cf.mean['cifar100'], cf.std['cifar100']),
    ])

    print("| Preparing CIFAR-100 dataset...")
    sys.stdout.write("| ")
    import glob
    print ("\ndata dir", args.datadir)
    print ("\ndata dir list: {}".format(glob.glob(os.path.join(args.datadir, "*"))))
    trainset = torchvision.datasets.CIFAR100(root=args.datadir, train=True, download=False, transform=transform_train)
    testset = torchvision.datasets.CIFAR100(root=args.datadir, train=False, download=False, transform=transform_test)
    num_classes = 100
elif args.dataset=='TinyImageNet':
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])
    print ("\ndata dir", args.datadir)
    testset = datasets.ImageFolder(os.path.join(args.datadir, 'val_cls'), transforms.Compose([
                transforms.Scale(64),
                transforms.CenterCrop(56),
                transforms.ToTensor(),
                normalize,
                ]))
    trainset = datasets.ImageFolder(os.path.join(args.datadir, 'train'), transforms.Compose([
                transforms.Scale(64),
                transforms.RandomCrop(56),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                normalize,
                ]))
    num_classes = 200


'''
2. Initialize Horovod distributed sampler
'''
train_sampler = torch.utils.data.distributed.DistributedSampler(trainset, num_replicas=hvd.size(), rank=hvd.rank())
trainloader = torch.utils.data.DataLoader(trainset, batch_size=batch_size, num_workers=1, sampler=train_sampler, pin_memory=True)
test_sampler = torch.utils.data.distributed.DistributedSampler(testset, num_replicas=hvd.size(), rank=hvd.rank())
testloader = torch.utils.data.DataLoader(testset, batch_size=batch_size, num_workers=1, sampler=test_sampler, pin_memory=True)

# Return network & file name
def getNetwork(args):
    if args.arch == 'WIDERESNET':
        net = Wide_ResNet(args.depth, args.widen_factor, args.dropout, num_classes)
        file_name = 'wide-resnet-'+str(args.depth)+'x'+str(args.widen_factor)
    elif args.arch == 'PRERESNET':
        net = preresnet(depth=args.depth, num_classes=num_classes)
        file_name = 'preresnet-'+str(args.depth)

    return net, file_name

# Test only option
if (args.testOnly):
    print('\n[Test Phase] : Model setup')
    assert os.path.isdir('checkpoint'), 'Error: No checkpoint directory found!'
    _, file_name = getNetwork(args)
    checkpoint = torch.load('./checkpoint/'+os.sep+file_name+'.t7')
    net = checkpoint['net']

    net = torch.nn.DataParallel(net, device_ids=range(torch.cuda.device_count()))
    net.cuda()
    cudnn.benchmark = True

    net.eval()
    test_loss = 0
    correct = 0
    total = 0

    for batch_idx, (inputs, targets) in enumerate(testloader):
        inputs, targets = inputs.cuda(), targets.cuda()
        inputs, targets = Variable(inputs, volatile=True), Variable(targets)
        outputs = net(inputs)

        _, predicted = torch.max(outputs.data, 1)
        total += targets.size(0)
        correct += predicted.eq(targets.data).cpu().sum()

    acc = 100.*correct/total
    print("| Test Result\tAcc@1: %.2f%%" %(acc))

    sys.exit(0)

# Model
print('\n[Phase 2] : Model setup')
if args.resume:
    # Load checkpoint
    print('| Resuming from checkpoint...')
    assert os.path.isdir('checkpoint'), 'Error: No checkpoint directory found!'
    _, file_name = getNetwork(args)
    checkpoint = torch.load('./checkpoint/'+os.sep+file_name+'.t7')
    net = checkpoint['net']
    best_acc = checkpoint['acc']
    start_epoch = checkpoint['epoch']
else:
    print('| Building net ...')
    net, file_name = getNetwork(args)
    net.apply(conv_init)

if args.multi_gpu:
    net = torch.nn.DataParallel(net, device_ids=range(torch.cuda.device_count()))
    cudnn.benchmark = True
net.cuda()

'''
3. Broadcast parameters, scale learning rate, compression, and distributed optimizer
'''
hvd.broadcast_parameters(net.state_dict(), root_rank=0)

criterion = nn.CrossEntropyLoss().cuda()

print ("initializing optimizer on node {}".format(hvd.local_rank()))
optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=0.9, weight_decay=5e-4)
optimizer = hvd.DistributedOptimizer(optimizer, named_parameters=net.named_parameters())

# Training
def train(epoch):
    net.train()
    train_loss = 0
    correct = 0
    total = 0

    print('\n=> Training Epoch #%d, LR=%.4f' %(epoch, cf.learning_rate(args.lr*batch_size, epoch, args.warmup_epoch, 0, len(trainloader), hvd.size())))
    for batch_idx, (inputs, targets) in enumerate(trainloader):
        lr = cf.learning_rate(args.lr*batch_size, epoch, args.warmup_epoch, batch_idx, len(trainloader), hvd.size())
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
        inputs, targets = inputs.cuda(), targets.cuda() # GPU settings
        optimizer.zero_grad()
        inputs, targets = Variable(inputs), Variable(targets)
        outputs = net(inputs)               # Forward Propagation
        loss = criterion(outputs, targets)  # Loss
        loss.backward()  # Backward Propagation
        optimizer.step() # Optimizer update

        train_loss += loss.data.item()
        _, predicted = torch.max(outputs.data, 1)
        total += targets.size(0)
        correct += predicted.eq(targets.data).cpu().sum()
        print ('| Epoch [%3d/%3d] Iter[%3d/%3d]\t\tLoss: %.4f Acc@1: %.3f%% LR: %.8f'
                %(epoch, num_epochs, batch_idx+1,
                    (len(trainset)//batch_size)+1, loss.data.item(), 100.*correct/total, lr))
    if hvd.rank()==0:
        save_dict = {"epoch": epoch, "optimizer": optimizer.state_dict(), "state_dict": net.state_dict()}
        torch.save(save_dict, os.path.join('/home/lunit/Pytorch-Horovod-Examples/examples/cifar100/checkpoints/', 'cifar100_last.pth.tar'))

def metric_average(val, name):
    tensor = torch.tensor(val)
    avg_tensor = hvd.allreduce(tensor, name=name)
    return avg_tensor.item()

def test(epoch):
    global best_acc
    net.eval()
    test_loss = 0
    correct = 0
    total = 0
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(testloader):
            inputs, targets = inputs.cuda(), targets.cuda()
            outputs = net(inputs)
            loss = criterion(outputs, targets)
 
            test_loss += loss.data.item()
            _, predicted = torch.max(outputs.data, 1)
            total += targets.size(0)
            correct += predicted.eq(targets.data).cpu().float().sum()
    test_loss /= len(test_sampler)
    test_accuracy = correct / len(test_sampler)
    test_loss = metric_average(test_loss, 'avg_loss')
    test_accuracy = metric_average(test_accuracy, 'avg_acc')
    if best_acc < test_accuracy:
        best_acc = test_accuracy
    if hvd.rank()==0:
        print ("\n| Validation average loss : {:.4f}, accuracy: {:.2f}%, best accuracy so far {:.2f}%\n".format(test_loss, 100.*test_accuracy, 100.*best_acc))

print('\n[Phase 3] : Training model')
print('| Training Epochs = ' + str(num_epochs))
print('| Initial Learning Rate = ' + str(args.lr))
print('| Optimizer = ' + str(optim_type))

elapsed_time = 0
for epoch in range(start_epoch, start_epoch+num_epochs+20):
    start_time = time.time()

    train(epoch)
    test(epoch)

    epoch_time = time.time() - start_time
    elapsed_time += epoch_time
    print('| Elapsed time : %d:%02d:%02d'  %(cf.get_hms(elapsed_time)))

print('\n[Phase 4] : Testing model')
print('* Test results : Acc@1 = %.2f%%' %(best_acc))
