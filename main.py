from __future__ import print_function, division
import argparse
import os
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
from torch.autograd import Variable
import torchvision.utils as vutils
import torch.nn.functional as F
import numpy as np
import time
from tensorboardX import SummaryWriter
from datasets import __datasets__
from models import __models__, model_loss ,silog_loss ,SegmentationLosses
from utils import *
from torch.utils.data import DataLoader
from math import isnan
import torchvision
import gc
import cv2 as cv
from os.path import join, split, isdir, isfile, splitext, split, abspath, dirname

import pytorch_warmup as warmup
import matplotlib.pyplot as plt
from pylab import *
import matplotlib
from models.bts import BtsModel
from models.deeplab_modeling.deeplab import *
matplotlib.use('pdf')


cudnn.benchmark = True

parser = argparse.ArgumentParser(description='Group-wise Correlation Stereo Network (GwcNet)')
parser.add_argument('--model', default='gwcnet-gc', help='select a model structure', choices=__models__.keys())
parser.add_argument('--maxdisp', type=int, default=192, help='maximum disparity')
parser.add_argument('--bf', type=float, default=720*0.54, help='baseline*focal length')
parser.add_argument('--start_epoch', type=int, default=-1, help='start_from_zero_epoch')

parser.add_argument('--dataset', required=True, help='dataset name', choices=__datasets__.keys())
parser.add_argument('--datapath', required=True, help='data path')
parser.add_argument('--trainlist', required=True, help='training list')
parser.add_argument('--testlist', required=True, help='testing list')

parser.add_argument('--lr', type=float, default=0.001, help='base learning rate')
parser.add_argument('--adam_eps', type=float, help='epsilon in mono Adam optimizer', default=1e-6)
parser.add_argument('--batch_size', type=int, default=1, help='training batch size')
parser.add_argument('--test_batch_size', type=int, default=1, help='testing batch size')
parser.add_argument('--epochs', type=int, required=True, help='number of epochs to train')
parser.add_argument('--lrepochs', type=str, required=True, help='the epochs to decay lr: the downscale rate')

parser.add_argument('--logdir', required=True, help='the directory to save logs and checkpoints')
parser.add_argument('--loadckpt', help='load the weights from a specific checkpoint')
#parser.add_argument('--resume', action='store_true', help='continue training the model')
parser.add_argument('--train', action='store_true', help='train the model')
parser.add_argument('--train_mono', action='store_true', help='train the mono model')
parser.add_argument('--train_bio', action='store_true', help='train the bio model')
parser.add_argument('--train_deeplab', action='store_true', help='train the deeplab model')
parser.add_argument('--start_from_zero_epoch', action='store_true', help='start_from_zero_epoch')
parser.add_argument('--resume', default=False, help='continue training the model')
parser.add_argument('--seed', type=int, default=1, metavar='S', help='random seed (default: 1)')

parser.add_argument('--summary_freq', type=int, default=20, help='the frequency of saving summary')
parser.add_argument('--save_freq', type=int, default=1, help='the frequency of saving checkpoint')

parser.add_argument('--mono_model_name', type=str, help='model name', default='bts_nyu_v2')
parser.add_argument('--mono_encoder', type=str, help='type of encoder, vgg or desenet121_bts or densenet161_bts',
                    default='densenet161_bts')
parser.add_argument('--mono_weight_decay',type=float, help='weight decay factor for optimization', default=1e-2)
parser.add_argument('--mono_input_height', type=int, help='input height', default=480)
parser.add_argument('--mono_input_width', type=int, help='input width', default=640)
parser.add_argument('--mono_max_depth', type=float, help='maximum depth in estimation', default=80)
parser.add_argument('--mono_checkpoint_path', type=str, help='path to a specific checkpoint to load', default='')
parser.add_argument('--sig_resume', type=str, help='path to a specific checkpoint to load', default='')
parser.add_argument('--do_kb_crop', help='if set, crop input images as kitti benchmark images', action='store_true')
parser.add_argument('--save_lpg', help='if set, save outputs from lpg layers', action='store_true')

parser.add_argument('--deeplab_backbone', type=str, default='resnet',
                        choices=['resnet', 'xception', 'drn', 'mobilenet'],
                        help='backbone name (default: resnet)')
parser.add_argument('--deeplab_out_stride', type=int, default=16,
                        help='network output stride (default: 8)')
parser.add_argument('--deeplab_sync_bn', type=bool, default=False,
                        help='whether to use sync bn (default: auto)')
parser.add_argument('--deeplab_freeze_bn', type=bool, default=False,
                        help='whether to freeze bn parameters (default: False)') 
parser.add_argument('--deeplab_loss_type', type=str, default='ce',
                        choices=['ce', 'focal'],
                        help='loss func type (default: ce)')                 

parser.add_argument('--r2l', help='if set, predict disp from right img to left img', action='store_true')
parser.add_argument('--make_occ_mask', help='if set, make occ mask', action='store_true')
parser.add_argument('--bts_size', type=int,   help='initial num_filters in bts', default=512)
parser.add_argument('--occlude', type=float,   help='occ rate in result', default=0.73)
parser.add_argument('--mask', type=float,   help='occ rate in result', default=0.2)
parser.add_argument('--sig_arg', type=float,   help='bbm rate', default=0)

parser.add_argument('--variance_focus',type=float, help='lambda in paper: [0, 1], higher value more focus on minimizing variance of error', default=0.85)
# parse arguments, set seeds
args = parser.parse_args()
torch.manual_seed(args.seed)
torch.cuda.manual_seed(args.seed)
os.makedirs(args.logdir, exist_ok=True)

# test_gt_path='/data/yyx/GwcNet-master/checkpoints/kitti/ft_from0/kitti_test_gt/'
# test_pred1_path='/data/yyx/GwcNet-master/checkpoints/kitti/ft_from0/kitti_test_pred/'
# os.makedirs(test_gt_path exist_ok=True)
# os.makedirs(test_pred1_path, exist_ok=True)

# create summary logger
print("creating new summary file")
# logger = SummaryWriter(args.logdir)
logger = SummaryWriter(comment='oh try with 2 batch')

# dataset, dataloader
StereoDataset = __datasets__[args.dataset]
train_dataset = StereoDataset(args.datapath, args.trainlist, True)
test_dataset = StereoDataset(args.datapath, args.testlist, False)
TrainImgLoader = DataLoader(train_dataset, args.batch_size, shuffle=True, num_workers=8, drop_last=True)
TestImgLoader = DataLoader(test_dataset, args.test_batch_size, shuffle=False, num_workers=4, drop_last=False)

# model, optimizer
model = __models__[args.model](args)
mono_model = BtsModel(params=args)
deeplab_model = DeepLab(num_classes=2,#classes n
                        backbone=args.deeplab_backbone,
                        output_stride=args.deeplab_out_stride,
                        sync_bn=args.deeplab_sync_bn,
                        freeze_bn=args.deeplab_freeze_bn)
# train_params = [{'params': model.get_1x_lr_params(), 'lr': args.lr},
#                 {'params': model.get_10x_lr_params(), 'lr': args.lr * 10}]
model = nn.DataParallel(model)
mono_model = nn.DataParallel(mono_model)
deeplab_model = nn.DataParallel(deeplab_model)
model.cuda()
mono_model.cuda()
deeplab_model.cuda()
#make optimizer
optimizer = optim.Adam(model.parameters(), lr=args.lr, betas=(0.9, 0.999))

# mono_optimizer = optim.Adam([{'params': mono_model.module.encoder.parameters(), 'mono_weight_decay': args.mono_weight_decay},
#                                 {'params': mono_model.module.decoder.parameters(), 'mono_weight_decay': 0}],
#                                 lr=args.lr, eps=args.adam_eps)
mono_optimizer = optim.Adam(mono_model.parameters(), lr=args.lr, betas=(0.9, 0.999))
deeplab_optimizer = optim.Adam(deeplab_model.parameters(), lr=args.lr, betas=(0.9, 0.999))

#mono loss
silog_criterion = silog_loss(variance_focus=args.variance_focus)
        
#deeplab sig loss      
deeplab_sig_criterion = SegmentationLosses(weight=None).build_loss(mode=args.deeplab_loss_type)




# for index,(name,value) in enumerate(model.named_parameters()):
#     #value.requires_grad = (index < last)
#     #value.requires_grad = False
#     print(index, name," : ",value.requires_grad)

# load parameters
start_epoch = 0
if args.resume:
    # find all checkpoints file and sort according to epoch id
    all_saved_ckpts = [fn for fn in os.listdir(args.logdir) if fn.endswith(".ckpt")]
    all_saved_ckpts = sorted(all_saved_ckpts, key=lambda x: int(x.split('_')[-1].split('.')[0]))
    # use the latest checkpoint file
    loadckpt = os.path.join(args.logdir, all_saved_ckpts[-1])
    print("loading the lastest model in logdir: {}".format(loadckpt))
    state_dict = torch.load(loadckpt)
    model.load_state_dict(state_dict['model'])
    optimizer.load_state_dict(state_dict['optimizer'])
    start_epoch = state_dict['epoch'] + 1
elif args.loadckpt:
    # load the checkpoint file specified by args.loadckpt
    print("loading model {}".format(args.loadckpt))
    state_dict = torch.load(args.loadckpt)
    model.load_state_dict(state_dict['model'])
    start_epoch = state_dict['epoch'] + 1
if args.mono_checkpoint_path:
    print("loading mono model {}".format(args.mono_checkpoint_path))
    mono_checkpoint = torch.load(args.mono_checkpoint_path)
    mono_model.load_state_dict(mono_checkpoint['model'])
if args.sig_resume:
    checkpoint = torch.load(args.sig_resume)
    args.start_epoch = checkpoint['epoch']
    deeplab_model.load_state_dict(checkpoint['model'])
    print("loading sig checkpoint '{}' (epoch {})"
            .format(args.sig_resume, checkpoint['epoch']))
print("start at epoch {}".format(start_epoch))

splits = args.lrepochs.split(':')
assert len(splits) == 2
# parse the epochs to downscale the learning rate (before :)
downscale_epochs = [int(eid_str) for eid_str in splits[0].split(',')]
# parse downscale rate (after :)
downscale_rate = float(splits[1])
print("downscale epochs: {}, downscale rate: {}".format(downscale_epochs, downscale_rate))
lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, downscale_epochs, gamma=(1/downscale_rate), last_epoch=-1)
warmup_scheduler = warmup.UntunedExponentialWarmup(optimizer)
warmup_scheduler.last_step = -1 # initialize the step counter

if args.start_epoch>=0:
    start_epoch = args.start_epoch
print("start_epoch",start_epoch)
def train():
    for epoch_idx in range(start_epoch, args.epochs):
        if args.train_bio:
            adjust_learning_rate(optimizer, epoch_idx, args.lr, args.lrepochs,lr_scheduler,warmup_scheduler)
        if args.train_mono:
            adjust_learning_rate(mono_optimizer, epoch_idx, args.lr, args.lrepochs,lr_scheduler,warmup_scheduler)
        if args.train_deeplab:
            adjust_learning_rate(deeplab_optimizer, epoch_idx, args.lr, args.lrepochs,lr_scheduler,warmup_scheduler)
        print("current rate is ",args.lr)
        print("maxdisp is ",args.maxdisp,", maxdepth is",args.mono_max_depth)
        #avg_train_scalars = AverageMeter()
        all_loss=0.0
        all_mono_loss=0.0
        all_sig_loss=0.0
        
        #print(all_loss)
        # training
        if args.train:
            assert args.train_mono or args.train_bio or args.train_deeplab
            nannum=0
            mono_nannum=0
            sig_nannum=0
            for batch_idx, sample in enumerate(TrainImgLoader):
                #print(len(sample))
                #print("!!!!!!!!!!!!!")
                global_step = len(TrainImgLoader) * epoch_idx + batch_idx
                start_time = time.time()
                do_summary = global_step % args.summary_freq == 0
 
                
                #loss_all=loss_all+loss
                if args.train_bio:
                    loss, scalar_outputs, image_outputs = train_sample(sample,compute_metrics=do_summary)
                    if math.isnan(loss):
                        print(batch_idx,'loss is nan')
                        nannum=nannum+1
                    else:
                        all_loss=loss+all_loss
                if args.train_mono:
                    mono_loss, scalar_outputs, image_outputs = train_sample(sample,compute_metrics=do_summary)
                    if math.isnan(mono_loss):
                        print(batch_idx,'mono loss is nan')
                        mono_nannum=mono_nannum+1
                    else:
                        all_mono_loss=mono_loss+all_mono_loss
                if args.train_deeplab:
                    sig_loss,sig_only_loss,mask_pixel,epe_loss,epe, scalar_outputs, image_outputs=sig_sample(sample,batch_idx,compute_metrics=do_summary)
                    if math.isnan(sig_loss):
                        print(batch_idx,'sig loss is nan')
                        sig_nannum=sig_nannum+1
                    else:
                        all_sig_loss=sig_loss+all_sig_loss
                if do_summary:
                    save_scalars(logger, 'train', scalar_outputs, global_step)
                    save_images(logger, 'train', image_outputs, global_step)

                #avg_train_scalars.update(loss)
                #print("!!!",avg_train_scalars)
                del scalar_outputs, image_outputs
                if args.train_bio:
                    if (batch_idx%100==0)or(batch_idx==len(TrainImgLoader)-1):
                        print('Epoch {}/{}, Iter {}/{},loss = {:.3f}, time = {:.3f},loss avg={:.3f}'.format(epoch_idx, args.epochs,
                                                                                        batch_idx,
                                                                                        len(TrainImgLoader), loss,
                                                                                        time.time() - start_time,all_loss/(batch_idx-nannum+1)))
                if args.train_mono:
                    if (batch_idx%100==0)or(batch_idx==len(TrainImgLoader)-1):
                        print('Epoch {}/{}, Iter {}/{},mono_loss = {:.3f}, time = {:.3f},mono loss avg={:.3f}'.format(epoch_idx, args.epochs,
                                                                                        batch_idx,
                                                                                        len(TrainImgLoader), mono_loss,
                                                                                        time.time() - start_time,all_mono_loss/(batch_idx-mono_nannum+1)))
                if args.train_deeplab:
                    if (batch_idx%100==0)or(batch_idx==len(TrainImgLoader)-1):
                        # tensor2float(all_loss),tensor2float(sig_pixel),tensor2float(origin_epe),tensor2float(refined_epe), tensor2float(scalar_outputs), image_outputs
                        print('Epoch {}/{}, Iter {}/{},sig_loss = {:.3f},sig_pixel = {:.3f},mask = {:.3f},origin_epe = {:.3f},refined_epe={:.3f}, time = {:.3f},sig loss avg={:.3f}'.format(epoch_idx, args.epochs,
                                                                                        batch_idx,
                                                                                        len(TrainImgLoader), sig_loss,sig_only_loss,mask_pixel,epe_loss,epe,
                                                                                        time.time() - start_time,all_sig_loss/(batch_idx-sig_nannum+1)))

                    # =================写到这儿,test也要改====================
            #loss_all=loss_all.avg()
            #avg_train_scalars=avg_train_scalars.mean()
            #print("avg_train_loss",all_loss/(len(TestImgLoader)-nannum))
                    #print("losses",loss_all/(batch_idx+1))
            #print("loss averange=",loss_all/12537.0)
            #saving checkpoints

            if (epoch_idx + 1) % args.save_freq == 0:
                
                if args.train_bio:
                    checkpoint_data = {'epoch': epoch_idx, 'model': model.state_dict(), 'optimizer': optimizer.state_dict()}
                    torch.save(checkpoint_data, "{}/bio_checkpoint_{:0>6}.ckpt".format(args.logdir, epoch_idx))
                if args.train_mono:
                    checkpoint_data = {'epoch': epoch_idx, 'model': mono_model.state_dict(), 'optimizer': mono_optimizer.state_dict()}
                    torch.save(checkpoint_data, "{}/mono_checkpoint_{:0>6}.ckpt".format(args.logdir, epoch_idx))
                if args.train_deeplab:
                    checkpoint_data = {'epoch': epoch_idx, 'model': deeplab_model.state_dict(), 'optimizer': deeplab_optimizer.state_dict()}
                    torch.save(checkpoint_data, "{}/sig_checkpoint_2_batch_desne_gt_{:0>6}.ckpt".format(args.logdir, epoch_idx))
            gc.collect()
            print("avg_train_loss",all_loss/(len(TrainImgLoader)-nannum))
        
        
        
        
        
        
        #testing
        # avg_test_scalars = AverageMeterDict()
        # for batch_idx, sample in enumerate(TestImgLoader):
        #     # print("test model")
        #     #print("sample",sample)
        #     global_step = len(TestImgLoader) * epoch_idx + batch_idx
        #     start_time = time.time()
        #     do_summary = global_step % args.summary_freq == 0
        #     loss, mono_loss, scalar_outputs, image_outputs = test_sample(batch_idx,sample, compute_metrics=do_summary)

        #     if do_summary:
        #         # print("is it here? in do summary?",len(image_outputs))
        #         save_scalars(logger, 'test', scalar_outputs, global_step)
        #         save_images(logger, 'test', image_outputs, global_step)
        #     avg_test_scalars.update(scalar_outputs)
        #     del scalar_outputs, image_outputs
        #     if (batch_idx%100==0)or(batch_idx==len(TestImgLoader)-1):
        #         print('Epoch {}/{}, Iter {}/{}, test loss = {:.3f},mono_loss = {:.3f}, time = {:3f}'.format(epoch_idx, args.epochs,
        #                                                                              batch_idx,
        #                                                                              len(TestImgLoader), loss, mono_loss ,
        #                                                                              time.time() - start_time))
        #     #print(avg_test_scalars["EPE"])
        # avg_test_scalars = avg_test_scalars.mean()
        # save_scalars(logger, 'fulltest', avg_test_scalars, len(TrainImgLoader) * (epoch_idx + 1))
        # print("avg_test_scalars", avg_test_scalars)
        # gc.collect()

def lr_consistency_map(disp_left ,disp_right):
    lr_cons=[]
    # for i in range(len(disp_left)):
    #     lr_cons.append(torch.abs(disp_left[i] - disp_right[i]))
    lr_con_unabs=disp_left - disp_right
    lr_con=torch.abs(lr_con_unabs)
    lr_cons.append(lr_con)
    return lr_cons

# train one sample
def sig_sample(sample,batch_idx, compute_metrics=False):
    model.eval()
    mono_model.eval()
    deeplab_model.train()
    
    relu=nn.ReLU()
    
    # model.train()
    imgL, imgR, disp_gt = sample['left'], sample['right'], sample['disparity']
    imgL = imgL.cuda()
    imgR = imgR.cuda()
    disp_gt = disp_gt.cuda()
    mask = (disp_gt < args.maxdisp) & (disp_gt > 0)
    deeplab_optimizer.zero_grad()
    # make mono and bio results
    disp_ests,confidence,confidence_var,index = model(imgL, imgR)
    disp_ests_final= disp_ests[-1]
    # print(disp_ests_final.shape)
    mono_depth =mono_model(imgL, imgR)
    mono_final_depth =mono_depth[-1]
    # print(mono_final_depth.shape)
    mono_final_depth=torch.squeeze(mono_final_depth,dim=1)
    # print(mono_final_depth.shape)
    # torch.Size([2, 1, 256, 512])
    mono_disp_est =args.bf / mono_final_depth

    #make sig-gt
    # print(mono_disp_est.shape,disp_gt.shape)
    mono=torch.abs(mono_disp_est-disp_gt)
    bio=torch.abs(disp_ests_final-disp_gt)
    mbb=((bio-mono)>0).float()*(mask.float())#m比b好为1
    bbm=((mono-bio)>0).float()*(mask.float())
    target=((bio-mono)>args.sig_arg).float()*(mask.float())
    
    # mbb=((bio-mono)>0).float()#m比b好为1
    # bbm=((mono-bio)>0).float()
    # # target=mbb#mbb为1
    # target=((bio-mono)>args.sig_arg)

    #prepare inputs
    disp_diff=disp_ests_final-mono_disp_est
    # print(disp_diff.shape,confidence.shape,imgL.shape)
    conf=torch.softmax(confidence_var,dim=1)
    conf=relu(0.4-conf)*2
    img_color=imgL
    img_singal=0.299*img_color[:,0,:,:]+0.587*img_color[:,1,:,:]+0.114*img_color[:,2,:,:]
    img_color_R=imgR
    img_singal_R=0.299*img_color_R[:,0,:,:]+0.587*img_color_R[:,1,:,:]+0.114*img_color_R[:,2,:,:]
    img_singal_usq=torch.unsqueeze(img_singal,dim=1)
    confidence_usq=torch.unsqueeze(confidence,dim=1)
    disp_diff_usq=torch.unsqueeze(disp_diff,dim=1)
    # print(confidence_usq.shape,disp_diff_usq.shape,img_singal_usq.shape)
    if len(disp_diff_usq.shape)==5:
        disp_diff_usq=disp_diff_usq[:,:,0,:,:]
    concat_diff=torch.cat([confidence_usq,disp_diff_usq,img_singal_usq],dim=1)
    # concat_diff=torch.unsqueeze()
    # print(concat_diff.shape)
    B,C,H,W=concat_diff.shape
    # concat_diff=concat_diff.repeat(2*B,C,H,W)
    # print(concat_diff.shape)
    #train sig deeplab model
    sig_output=deeplab_model(concat_diff)
    # print(sig_output.shape)
    sig_map=torch.argmax(sig_output,dim=1).float()
    # sig_map=sig_output[:,0,:,:].float()-sig_output[:,1,:,:].float()
    # sig_map=((torch.sigmoid(sig_map)*2-1)<0).float()
    sig_pixel=torch.sum(sig_map)
    # print(sig_output.shape,target.shape)
    sig_loss=deeplab_sig_criterion(sig_output,mbb)
    # print(mbb.long())

    
    
    # print(sig_map,sig_loss)
    scalar_outputs = {"sig loss": sig_loss}
    image_outputs = {"sig output": sig_map,"sig_gt":mbb, "sig_gt_small": target,"confidence":confidence,"confidence_var":confidence_var, "imgL": imgL, "imgR": imgR}
 
        #作用遮罩

        # disp_ests_unocc=disp_ests_final*mask_occ
        # mono_disp_ests_occ=mono_disp_ests*mask_occ_re
    disp_ests_unocc=disp_ests_final*bbm
    mono_disp_est_occ=mono_disp_est*mbb
    # print(disp_ests_unocc.shape,mono_disp_ests_occ.shape)
    disp_est_refine=disp_ests_unocc+mono_disp_est_occ
    
    #预测原始epe
    origin_epe=EPE_metric(disp_ests_final, disp_gt, mask)
    gt_epe=EPE_metric(disp_est_refine, disp_gt, mask)
    scalar_outputs["original EPE"] = [EPE_metric(disp_ests_final, disp_gt, mask)]
    image_outputs["original errormap"] = [disp_error_image_func()(disp_ests_final, disp_gt)]
    image_outputs["out of z mask"] = [mask.float()]
    
    
    #作用预测的遮罩
    sig_map_mbb=sig_map
    sig_map_bbm=1-sig_map
    disp_est_pred_unocc=disp_ests_final*sig_map_bbm
    mono_disp_est_pred_occ=mono_disp_est*sig_map_mbb
    disp_est_pred_refine= disp_est_pred_unocc+mono_disp_est_pred_occ
    
    refined_epe=EPE_metric(disp_est_pred_refine, disp_gt, mask)
    scalar_outputs["sig refined EPE"] = [EPE_metric(disp_est_pred_refine, disp_gt, mask)]
    image_outputs["sig refine errormap"] = [disp_error_image_func()(disp_est_pred_refine, disp_gt)]
    
    scalar_outputs["sig refine and sig-gt refine EPE"] = [EPE_metric(disp_est_pred_refine, disp_est_refine, mask)]
    image_outputs["sig refine and sig-gt refine errormap"] = [disp_error_image_func()(disp_est_pred_refine, disp_est_refine)]
    
    scalar_outputs["sig and sig-gt EPE"] = [EPE_metric(sig_map, mbb, mask)]
    image_outputs["sig and sig-gt errormap"] = [disp_error_image_func()(sig_map, mbb)]
    image_outputs["sig-gt and sig errormap"] = [disp_error_image_func()(mbb, sig_map)]
    image_outputs["sig有 sig-gt 没有"] = [relu(sig_map-mbb)]
    
    #增加D1指标
    
    scalar_outputs["original-pred_refined"] = [EPE_metric(disp_ests_final, disp_gt, mask)-EPE_metric(disp_est_pred_refine, disp_gt, mask)]
    scalar_outputs["original-gt_refined"] = [EPE_metric(disp_ests_final, disp_gt, mask)-EPE_metric(disp_est_refine, disp_gt, mask)]
    scalar_outputs["(original-gt_refined)-(original-pred_refined)"] = [(EPE_metric(disp_ests_final, disp_gt, mask)-EPE_metric(disp_est_refine, disp_gt, mask))-(EPE_metric(disp_ests_final, disp_gt, mask)-EPE_metric(disp_est_pred_refine, disp_gt, mask))]
    # disp_est_origin=disp_ests[-1]
    # disp_ests[-1]=disp_est_pred_refine
    
    # epe_loss = F.smooth_l1_loss(disp_est_pred_refine[mask], disp_est_refine[mask], size_average=True)*5
    epe_loss = F.smooth_l1_loss(sig_map, mbb, size_average=True)
    scalar_outputs["loss EPE"] = [epe_loss]
    loss_weight=0.85
    all_loss=loss_weight*sig_loss+(1-loss_weight)*epe_loss
    # epe_loss.backward()
    how_much_better=torch.sum(EPE_metric(disp_ests_final, disp_gt, mask))-torch.sum(EPE_metric(disp_est_pred_refine, disp_gt, mask))
    mask_pixel=torch.sum(mask.float())
    if how_much_better>=0:
        sample_idx=batch_idx
        save_sample_path="{}/good_sample/{}".format(args.logdir,sample_idx)
        while os.path.exists(save_sample_path) :
            sample_idx+=1
            save_sample_path="{}/good_sample/{}".format(args.logdir,sample_idx)
        save_sample_path=save_sample_path+"-{:.4f}".format(tensor2float(how_much_better))
        os.makedirs(save_sample_path, exist_ok=True)
        torchvision.utils.save_image(imgL,join(save_sample_path, "imgL-%d.jpg" % sample_idx))
        torchvision.utils.save_image(imgR,join(save_sample_path, "imgR-%d.jpg" % sample_idx))
        # torchvision.utils.save_image(disp_gt.repeat(1,3,1,1),join(save_sample_path, "disp-gt-%d.jpg" % sample_idx))
        
        gt=disp_gt
        disp_gt = np.array(disp_gt[0, :, :].cpu(), dtype=np.uint16)
        cv.imwrite(join(save_sample_path, "disp-gt-%d.png" % sample_idx), disp_gt * 255)
        # torchvision.utils.save_image(mbb,join(save_sample_path, "mbb-%d.jpg" % sample_idx))
        mbb = np.array(mbb[0, :, :].cpu(), dtype=np.uint8)
        cv.imwrite(join(save_sample_path, "mbb-%d.png" % sample_idx), mbb * 255)
        # torchvision.utils.save_image(sig_map,join(save_sample_path, "sig-map-%d.jpg" % sample_idx))
        sig_map=np.array(sig_map[0, :, :].cpu(), dtype=np.uint8)
        cv.imwrite(join(save_sample_path, "sig-map-%d.png" % sample_idx), sig_map * 255)
        disp_errormap=disp_error_image_func()(disp_est_pred_refine, gt)
        
        # disp_gt = np.array(disp_gt[1, :, :].cpu(), dtype=np.uint8)
        # cv.imwrite(join(save_sample_path, "disp-gt2-%d.png" % sample_idx), disp_gt * 255)
        # # torchvision.utils.save_image(mbb,join(save_sample_path, "mbb-%d.jpg" % sample_idx))
        # mbb = np.array(mbb[1, :, :].cpu(), dtype=np.uint8)
        # cv.imwrite(join(save_sample_path, "mbb2-%d.png" % sample_idx), mbb * 255)
        # # torchvision.utils.save_image(sig_map,join(save_sample_path, "sig-map-%d.jpg" % sample_idx))
        # sig_map=np.array(sig_map[1, :, :].cpu(), dtype=np.uint8)
        # cv.imwrite(join(save_sample_path, "sig-map2-%d.png" % sample_idx), sig_map * 255)
        # disp_errormap=disp_error_image_func()(disp_est_pred_refine, gt)
        # print(disp_errormap)
        # print(len(disp_errormap))
        # print(type(disp_errormap))
        # print(disp_errormap.shape)
        torchvision.utils.save_image(disp_errormap,join(save_sample_path, "sig_refine_errormap-%d.jpg" % sample_idx))
        
    all_loss.backward()
    deeplab_optimizer.step()
    return  tensor2float(all_loss),tensor2float(sig_pixel),tensor2float(mask_pixel),tensor2float(origin_epe),tensor2float(refined_epe),tensor2float(scalar_outputs), image_outputs


def train_sample(sample, compute_metrics=False):
    # model.eval()    # model.eval()
    if args.train_bio:
        model.train()
    if args.train_bio:
        mono_model.train()
    imgL, imgR, disp_gt = sample['left'], sample['right'], sample['disparity']
    imgL = imgL.cuda()
    imgR = imgR.cuda()
    disp_gt = disp_gt.cuda()
    mask = (disp_gt < args.maxdisp) & (disp_gt > 0)
    optimizer.zero_grad()
    mono_optimizer.zero_grad()
    if args.train_bio:
        disp_ests = model(imgL, imgR)
        disp_ests_final= disp_ests[-1]
            #print(len(disp_ests))
        #print(disp_ests[0].shape)
        #print("##########",mask.shape)
        loss = model_loss(disp_ests, disp_gt, mask)
        scalar_outputs = {"loss": loss}
        image_outputs = {"disp_est": disp_ests, "disp_gt": disp_gt, "imgL": imgL, "imgR": imgR}

    #mono
    if args.train_mono:
        mono_all_depth_ests =mono_model(imgL, imgR)
        depth_8x8_scaled, depth_4x4_scaled, depth_2x2_scaled, reduc1x1, mono_final_depth =mono_all_depth_ests
        mono_final_depth=torch.squeeze(mono_final_depth,dim=0)
        mono_disp_est =args.bf / mono_final_depth
        depth_gt=args.bf /disp_gt
        if args.dataset == "sceneflow":
            depth_mask = depth_gt > 0.1
        else :
            depth_mask = depth_gt > 1.0
        mono_loss = silog_criterion.forward(mono_final_depth, depth_gt, depth_mask)
        scalar_outputs = {"mono loss": mono_loss}
        image_outputs = {"mono_disp_est": mono_disp_est, "disp_gt": disp_gt, "imgL": imgL, "imgR": imgR}
    if args.make_occ_mask:
        if args.r2l==False:
            args.r2l=True
            disp_ests_r2l = model(imgR, imgL)
            args.r2l=False
        else:
            args.r2l=False
            disp_ests_r2l = model(imgR, imgL)
            args.r2l=True
        disp_ests_final_r2l=disp_ests_r2l[-1]
        lr_consistency=lr_consistency_map(disp_ests_final,disp_ests_final_r2l)
        mask_occ=(lr_consistency[-1][0, :, :]>args.occlude)
        mask_occ_re=(lr_consistency[-1][0, :, :]<=args.occlude)
        disp_ests_unocc=disp_ests_final[mask_occ_re]
        mono_disp_est_occ=mono_disp_est[mask_occ]
        disp_est_occ_refine=disp_ests_unocc+mono_disp_est_occ
        disp_ests[-1]=disp_est_occ_refine


    #loss = model_loss(disp_ests, disp_gt, mask,imgL, imgR)


    # print("mono loss",mono_loss)


    
    if compute_metrics:
        with torch.no_grad():
            # print(len(disp_ests))
            if args.train_bio:
                image_outputs["errormap"] = [disp_error_image_func()(disp_est, disp_gt) for disp_est in disp_ests]
                scalar_outputs["EPE"] = [EPE_metric(disp_est, disp_gt, mask) for disp_est in disp_ests]
                scalar_outputs["D1"] = [D1_metric(disp_est, disp_gt, mask) for disp_est in disp_ests]
                scalar_outputs["Thres1"] = [Thres_metric(disp_est, disp_gt, mask, 1.0) for disp_est in disp_ests]
                scalar_outputs["Thres2"] = [Thres_metric(disp_est, disp_gt, mask, 2.0) for disp_est in disp_ests]
                scalar_outputs["Thres3"] = [Thres_metric(disp_est, disp_gt, mask, 3.0) for disp_est in disp_ests]
            if args.train_mono:
                scalar_outputs["mono D1"] = [D1_metric(mono_disp_est, disp_gt, mask)]
                scalar_outputs["mono EPE"] = [EPE_metric(mono_disp_est, disp_gt, mask)]
                image_outputs["mono errormap"] = [disp_error_image_func()(mono_disp_est, disp_gt)]



   
    if args.train_bio:
        loss.backward()
        optimizer.step()
        return  tensor2float(loss), tensor2float(scalar_outputs), image_outputs
    if args.train_mono:
        mono_loss.backward()
        mono_optimizer.step()
        return  tensor2float(mono_loss), tensor2float(scalar_outputs), image_outputs






# test one sample
@make_nograd_func
def test_sample(batch_idx,sample, compute_metrics=True):
    model.eval()
    mono_model.eval()
    #model.train()

    imgL, imgR, disp_gt = sample['left'], sample['right'], sample['disparity']
    imgL = imgL.cuda()
    imgL = imgL.cuda()
    imgR = imgR.cuda()
    disp_gt = disp_gt.cuda()
    disp_ests,confidence,confidence_var,index = model(imgL, imgR)
    disp_ests_final= disp_ests[-1]
    mask = (disp_gt < args.maxdisp) & (disp_gt > 0)
    # print(confidence.max(),confidence.min(),confidence.mean())
    mono_depth =mono_model(imgL, imgR)
    mono_final_depth =mono_depth[-1]
    mono_final_depth=torch.squeeze(mono_final_depth,dim=0)
    mono_disp_est =args.bf / mono_final_depth
    depth_gt=args.bf/disp_gt
    mono_loss = silog_criterion.forward(mono_final_depth, depth_gt, mask)
    
    #prepare inputs
    disp_diff=disp_ests_final-mono_disp_est
    # print(disp_diff.shape,confidence.shape,imgL.shape)
    img_color=imgL
    img_singal=0.299*img_color[:,0,:,:]+0.587*img_color[:,1,:,:]+0.114*img_color[:,2,:,:]
    img_singal_usq=torch.unsqueeze(img_singal,dim=1)
    confidence_usq=torch.unsqueeze(confidence,dim=1)
    disp_diff_usq=torch.unsqueeze(disp_diff,dim=1)
    # print(confidence_usq.shape,disp_diff_usq.shape,img_singal_usq.shape)
    
    
    
    #make sig-gt
    mono=torch.abs(mono_disp_est-disp_gt)
    bio=torch.abs(disp_ests_final-disp_gt)
    mbb=((bio-mono)>0).float()*(mask.float())#m比b好为1
    bbm=((mono-bio)>0).float()*(mask.float())
    # target=mbb#mbb为1
    target=((bio-mono)>args.sig_arg).float()*(mask.float())

    if len(disp_diff_usq.shape)==5:
        disp_diff_usq=disp_diff_usq[:,:,0,:,:]
    concat_diff=torch.cat([confidence_usq,disp_diff_usq,img_singal_usq],dim=1)
    # concat_diff=torch.unsqueeze()
    # print(concat_diff.shape)
    B,C,H,W=concat_diff.shape
    # concat_diff=concat_diff.repeat(2*B,C,H,W)
    # print(concat_diff.shape)
    #train sig deeplab model
    sig_output=deeplab_model(concat_diff)
    # print(sig_output.shape)
    sig_map_original=torch.argmax(sig_output,dim=1).float()
    sig_map=sig_output[:,0,:,:].float()-sig_output[:,1,:,:].float()
    # sig_map=1-(torch.sigmoid(sig_map)).float()
    sig_map=((torch.sigmoid(sig_map)*2-1)<0).float()
    # print(sig_map_original.shape,sig_map.shape)
    sig_loss=deeplab_sig_criterion(sig_output, target)
    
    
    if args.make_occ_mask:
        # print("occ mask")
        if args.r2l==False:
            args.r2l=True
            disp_ests_r2l,_ = model(imgR, imgL)
            args.r2l=False
        else:
            args.r2l=False
            disp_ests_r2l,_ = model(imgR, imgL)
            args.r2l=True
        disp_ests_final_r2l=disp_ests_r2l[-1]
        # print("disp type--------------",type(disp_ests_final),disp_ests_final.shape,disp_ests_final.dtype,type(disp_ests_final_r2l),disp_ests_final_r2l.shape,disp_ests_final_r2l.dtype)
        a=disp_ests_final.cuda()-disp_ests_final_r2l.cuda()
        
        #一致性遮挡并取阈值
        lr_consistency=lr_consistency_map(disp_ests_final.cuda(),disp_ests_final_r2l.cuda())
        mask_occ=(lr_consistency[-1]>args.occlude).float()
        mask_occ_re=(lr_consistency[-1]<=args.occlude).float()
        
        #建模的伪更优点
        bio_sub_mono=disp_ests_final-mono_disp_est
        mono_sub_bio=mono_disp_est-disp_ests_final
        k=bio_sub_mono
        bbm_pred_unrelu=k.pow(2)+4*confidence*k
        bbm_pred=relu(bbm_pred_unrelu)

        mbb_pred=relu(-bbm_pred_unrelu)
        
        print("bbm_pred",bbm_pred.mean())
        a=torch.sum((bbm_pred>0).float())
        b=torch.sum((mbb_pred>0).float())
        print(a+b,b/(a+b))
        print()
        print(a,"------",b,"------")

        #伪遮罩取阈值
        mask_mbb_pred=(mbb_pred>args.mask).float()
        mask_bbm_pred=(bbm_pred>args.mask).float()
        
    #0-1遮罩的生成
    mono=torch.abs(mono_disp_est-disp_gt)
    bio=torch.abs(disp_ests_final-disp_gt)
    mbb=(bio>mono).float()#m比b好
    bbm=(bio<mono).float()

    sum_mbb=torch.sum(mbb)
    sum_bbm=torch.sum(bbm)
    # print(sum_mbb+sum_bbm)
    # print("GT",sum_mbb/(sum_mbb+sum_bbm),"---")
    #还原浮点遮罩
    relu=nn.ReLU()
    mbb_float=relu(bio-mono)
    bbm_float=relu(mono-bio)

    
    #作用遮罩
    # disp_ests_unocc=disp_ests_final*mask_occ
    # mono_disp_ests_occ=mono_disp_ests*mask_occ_re
    disp_ests_unocc_gt=disp_ests_final*bbm
    mono_disp_est_occ_gt=mono_disp_est*mbb
    disp_est_gt_refine= disp_ests_unocc_gt+mono_disp_est_occ_gt
    # print(disp_ests_unocc.shape,mono_disp_ests_occ.shape)


    sig_map_mbb=sig_map
    sig_map_bbm=1-sig_map
    disp_est_pred_unocc=disp_ests_final*sig_map_bbm
    mono_disp_est_pred_occ=mono_disp_est*sig_map_mbb
    disp_est_pred_refine= disp_est_pred_unocc+mono_disp_est_pred_occ

    disp_est_origin=disp_ests[-1]
    disp_ests[-1]=disp_est_pred_refine

    loss = model_loss(disp_ests, disp_gt,mask)
    #loss = model_loss(disp_ests, disp_gt,mask,imgL, imgR)
    #print(disp_ests[-1].dtype,index.dtype)
    if args.make_occ_mask:
        image_outputs = {"disp_est": disp_ests, "disp_gt": disp_gt, "imgL": imgL, "imgR": imgR, "mono_better_than_bio": mbb, "mask_occ": mask_occ,"confidence":confidence,"bbm_float":bbm_float,"mbb_float":mbb_float,"bbm_pred":bbm_pred,"mbb_pred":mbb_pred}
        image_outputs["errormap_origin"] = [disp_error_image_func()(disp_est_origin, disp_gt)]
    else:
        image_outputs = {"disp_est": disp_ests, "disp_gt": disp_gt, "imgL": imgL, "imgR": imgR}
    # print(disp_ests[-1].shape,mono_disp_est.shape)
    scalar_outputs = {"loss": loss}
    image_outputs["errormap"] = [disp_error_image_func()(disp_ests[-1], disp_gt)]
    scalar_outputs["EPE"] = [EPE_metric(disp_ests[-1], disp_gt, mask)]
    image_outputs["mono errormap"] = [disp_error_image_func()(mono_disp_est, disp_gt)]
    scalar_outputs["mono EPE"] = [EPE_metric(mono_disp_est, disp_gt, mask)]
    scalar_outputs["mono depth EPE"] = [EPE_metric(mono_final_depth, depth_gt, mask)]
    scalar_outputs["mono D1"] = [D1_metric(mono_disp_est, disp_gt, mask)]
    scalar_outputs["D1"] = [D1_metric(disp_ests[-1], disp_gt, mask)]
    scalar_outputs["D3"] = [D3_metric(disp_ests[-1], disp_gt, mask)]
    scalar_outputs["D1_origin"] = [D1_metric(disp_est_origin, disp_gt, mask)]
    scalar_outputs["D3_origin"] = [D3_metric(disp_est_origin, disp_gt, mask)]
    scalar_outputs["D1_refine_gt"] = [D1_metric(disp_est_gt_refine, disp_gt, mask)]
    scalar_outputs["D3_refine_gt"] = [D3_metric(disp_est_gt_refine, disp_gt, mask)]
    scalar_outputs["Thres1"] = [Thres_metric(disp_ests[-1], disp_gt, mask, 1.0)]
    scalar_outputs["Thres2"] = [Thres_metric(disp_ests[-1], disp_gt, mask, 2.0)]
    scalar_outputs["Thres3"] = [Thres_metric(disp_ests[-1], disp_gt, mask, 3.0)]

    if compute_metrics:
        image_outputs["errormap"] = [disp_error_image_func()(disp_est, disp_gt) for disp_est in disp_ests]

    return tensor2float(loss), tensor2float(mono_loss), tensor2float(scalar_outputs), image_outputs




# # test one sample
# @make_nograd_func
# def test_sample(batch_idx,sample, compute_metrics=True):
#     model.eval()
#     mono_model.eval()
#     #model.train()

#     imgL, imgR, disp_gt = sample['left'], sample['right'], sample['disparity']
#     imgL = imgL.cuda()
#     imgL = imgL.cuda()
#     imgR = imgR.cuda()
#     disp_gt = disp_gt.cuda()
#     disp_ests,confidence,index = model(imgL, imgR)
#     disp_ests_final= disp_ests[-1]
#     mask = (disp_gt < args.maxdisp) & (disp_gt > 0)
#     # print(confidence.max(),confidence.min(),confidence.mean())
#     mono_depth =mono_model(imgL, imgR)
#     mono_final_depth =mono_depth[-1]
#     mono_final_depth=torch.squeeze(mono_final_depth,dim=0)
#     mono_disp_est =args.bf / mono_final_depth
#     mono_loss = silog_criterion.forward(mono_final_depth, disp_gt, mask)
#     if args.make_occ_mask:
#         # print("occ mask")
#         if args.r2l==False:
#             args.r2l=True
#             disp_ests_r2l,_,_ = model(imgR, imgL)
#             args.r2l=False
#         else:
#             args.r2l=False
#             disp_ests_r2l,_,_ = model(imgR, imgL)
#             args.r2l=True
#         disp_ests_final_r2l=disp_ests_r2l[-1]
#         # print("disp type--------------",type(disp_ests_final),disp_ests_final.shape,disp_ests_final.dtype,type(disp_ests_final_r2l),disp_ests_final_r2l.shape,disp_ests_final_r2l.dtype)
#         a=disp_ests_final.cuda()-disp_ests_final_r2l.cuda()
        
#         #一致性遮挡并取阈值
#         lr_consistency=lr_consistency_map(disp_ests_final.cuda(),disp_ests_final_r2l.cuda())
#         mask_occ=(lr_consistency[-1]>args.occlude).float()
#         mask_occ_re=(lr_consistency[-1]<=args.occlude).float()
        
#         #0-1遮罩的生成
#         mono=torch.abs(mono_disp_est-disp_gt)
#         bio=torch.abs(disp_ests_final-disp_gt)
#         mbb=(bio>mono).float()#m比b好
#         bbm=(bio<mono).float()

#         sum_mbb=torch.sum(mbb)
#         sum_bbm=torch.sum(bbm)
#         # print(sum_mbb+sum_bbm)
#         # print("GT",sum_mbb/(sum_mbb+sum_bbm),"---")
#         #还原浮点遮罩
#         relu=nn.ReLU()
#         mbb_float=relu(bio-mono)
#         bbm_float=relu(mono-bio)

#         #建模的伪更优点
#         bio_sub_mono=disp_ests_final-mono_disp_est
#         mono_sub_bio=mono_disp_est-disp_ests_final
#         k=bio_sub_mono
#         bbm_pred_unrelu=k.pow(2)+4*confidence*k
#         bbm_pred=relu(bbm_pred_unrelu)

#         mbb_pred=relu(-bbm_pred_unrelu)
        
#         # print("bbm_pred",bbm_pred.mean())
#         a=torch.sum((bbm_pred>0).float())
#         b=torch.sum((mbb_pred>0).float())
#         # print(a+b,b/(a+b))
#         # print()
#         # print(a,"------",b,"------")

#         #伪遮罩取阈值
#         mask_mbb_pred=(mbb_pred>args.mask).float()
#         mask_bbm_pred=(bbm_pred>args.mask).float()
        
#         #作用遮罩

#         # disp_ests_unocc=disp_ests_final*mask_occ
#         # mono_disp_ests_occ=mono_disp_ests*mask_occ_re
#         disp_ests_unocc=disp_ests_final*bbm
#         mono_disp_est_occ=mono_disp_est*mbb
#         # print(disp_ests_unocc.shape,mono_disp_ests_occ.shape)
#         disp_est_occ_refine=disp_ests_unocc+mono_disp_est_occ
#         disp_est_origin=disp_ests[-1]
#         disp_ests[-1]=disp_est_occ_refine



        


#     #disp_ests = model(imgL, imgR)
#     # print("left_fea",left_fea[0].shape)
#     # print("right_fea",right_fea[0].shape)
#     # print("cost",cost.shape)
#     # disp_gt= F.upsample(torch.unsqueeze(disp_gt,dim=0),[disp_ests[0].size()[1], disp_ests[0].size()[2]], mode='bilinear')
#     # disp_gt=torch.squeeze(disp_gt,dim=0)
#     # print(disp_gt.size())
    

#     #print(mask.shape)
#     #select_fea_scale=left_fea[0]
#     #svisualize_feature_map(select_fea_scale)
#     #disp_ests[0][mask==0]=0
    
#     #fea store
#     # iml_2=F.upsample(imgL, [select_fea_scale.size()[2], select_fea_scale.size()[3]], mode='bilinear')
#     # left_feature_path='/data/yyx/GwcNet-master/checkpoints/sceneflow_monkaa/gwcnet-gc/left_feature/'
#     # _, H, W = select_fea_scale[0].shape
#     # im_fea = torch.zeros(( H, W))
#     # for f in range(select_fea_scale.size()[1]):
#     #     im_fea = np.array(select_fea_scale[0][f,:, :].cpu()*255, dtype=np.uint16)
#     #     cv.imwrite(join(left_feature_path, "left_feature-%d.png" % (f+1)),im_fea)
#     # #im = np.array(iml_2[0,:,:,:].permute(1,2,0).cpu()*255, dtype=np.uint8)
#     # im = np.array(iml_2[0,:,:,:].permute(1,2,0).cpu()*255, dtype=np.uint8)
#     # cv.imwrite(join(left_feature_path, "itercolor-%d.jpg" % batch_idx),im)



#     #driving test
#     # test_gt_path='/data/yyx/GwcNet-master/checkpoints/sceneflow_monkaa/gwcnet-gc-gray/sceneflow_test_gt/'
#     # test_pred1_path='/data/yyx/GwcNet-master/checkpoints/sceneflow_monkaa/gwcnet-gc-gray/sceneflow_test_pred/'
        
#     # _, H, W = disp_ests[0].shape
#     # gt=torch.zeros((H,W))
#     # im_pred1 = torch.zeros(( H, W))

#     # gt = np.array(disp_gt[0,:, :].cpu()*2, dtype=np.uint16)
#     # cv.imwrite(join(test_gt_path, "sceneflow-gt-%d.png" % batch_idx),gt)
#     # im_pred1 = np.array(disp_ests[3][0,:, :].cpu()*2, dtype=np.uint16)
#     # cv.imwrite(join(test_pred1_path, "sceneflow-%d.png" % batch_idx),im_pred1)

#     #kitti test
#     # test_gt_path='/data/yyx/GwcNet-master/checkpoints/kitti/pri_192/kitti_test_gt/'
#     # test_pred1_path='/data/yyx/GwcNet-master/checkpoints/kitti/pri_192/kitti_test_pred/'
    
#     # # # os.makedirs(test_gt_path exist_ok=True)
#     # # # os.makedirs(test_pred1_path, exist_ok=True)
#     # _, H, W = disp_ests[0].shape
#     # gt=torch.zeros((H,W))
#     # im_pred1 = torch.zeros(( H, W))

#     # gt = np.array(disp_gt[0,:, :].cpu()*256 +1, dtype=np.uint16)
#     # cv.imwrite(join(test_gt_path,  "kitti-gt-%d.png" % batch_idx),gt)
#     # im_pred1 = np.array(disp_ests[0][0,:, :].cpu()*256, dtype=np.uint16)
#     # cv.imwrite(join(test_pred1_path,  "kitti-pred-%d.png" % batch_idx),im_pred1)
    

#     #disp_ests[-1]=index.float()
    
#     loss = model_loss(disp_ests, disp_gt,mask)
#     #loss = model_loss(disp_ests, disp_gt,mask,imgL, imgR)
#     #print(disp_ests[-1].dtype,index.dtype)
#     if args.make_occ_mask:
#         image_outputs = {"disp_est": disp_ests, "disp_gt": disp_gt, "imgL": imgL, "imgR": imgR, "mono_better_than_bio": mbb, "mask_occ": mask_occ,"confidence":confidence,"bbm_float":bbm_float,"mbb_float":mbb_float,"bbm_pred":bbm_pred,"mbb_pred":mbb_pred}
#         image_outputs["errormap_origin"] = [disp_error_image_func()(disp_est_origin, disp_gt)]
#     else:
#         image_outputs = {"disp_est": disp_ests, "disp_gt": disp_gt, "imgL": imgL, "imgR": imgR}
#     # print(disp_ests[-1].shape,mono_disp_est.shape)
#     scalar_outputs = {"loss": loss}
#     image_outputs["errormap"] = [disp_error_image_func()(disp_ests[-1], disp_gt)]
#     scalar_outputs["EPE"] = [EPE_metric(disp_ests[-1], disp_gt, mask)]
#     image_outputs["mono errormap"] = [disp_error_image_func()(mono_disp_est, disp_gt)]
#     scalar_outputs["mono EPE"] = [EPE_metric(mono_disp_est, disp_gt, mask)]
#     scalar_outputs["mono D1"] = [D1_metric(mono_disp_est, disp_gt, mask)]
#     scalar_outputs["D1"] = [D1_metric(disp_ests[-1], disp_gt, mask)]
#     scalar_outputs["D3"] = [D3_metric(disp_ests[-1], disp_gt, mask)]
#     scalar_outputs["Thres1"] = [Thres_metric(disp_ests[-1], disp_gt, mask, 1.0)]
#     scalar_outputs["Thres2"] = [Thres_metric(disp_ests[-1], disp_gt, mask, 2.0)]
#     scalar_outputs["Thres3"] = [Thres_metric(disp_ests[-1], disp_gt, mask, 3.0)]
#     # scalar_outputs["D1"] = [D1_metric(disp_est, disp_gt, mask) for disp_est in disp_ests]
#     # scalar_outputs["D3"] = [D3_metric(disp_est, disp_gt, mask) for disp_est in disp_ests]
#     # scalar_outputs["EPE"] = [EPE_metric(disp_est, disp_gt, mask) for disp_est in disp_ests]
#     # scalar_outputs["Thres1"] = [Thres_metric(disp_est, disp_gt, mask, 1.0) for disp_est in disp_ests]
#     # scalar_outputs["Thres2"] = [Thres_metric(disp_est, disp_gt, mask, 2.0) for disp_est in disp_ests]
#     # scalar_outputs["Thres3"] = [Thres_metric(disp_est, disp_gt, mask, 3.0) for disp_est in disp_ests]

#     # scalar_outputs = {"loss": loss}
#     # image_outputs = {"disp_est": disp_ests, "disp_gt": disp_gt, "imgL": imgL, "imgR": imgR}

#     # scalar_outputs["D1"] = [D1_metric(disp_est, disp_gt, mask) for disp_est in disp_ests]
#     # scalar_outputs["D3"] = [D3_metric(disp_est, disp_gt, mask) for disp_est in disp_ests]
#     # scalar_outputs["EPE"] = [EPE_metric(disp_est, disp_gt, mask) for disp_est in disp_ests]
#     # scalar_outputs["Thres1"] = [Thres_metric(disp_est, disp_gt, mask, 1.0) for disp_est in disp_ests]
#     # scalar_outputs["Thres2"] = [Thres_metric(disp_est, disp_gt, mask, 2.0) for disp_est in disp_ests]
#     # scalar_outputs["Thres3"] = [Thres_metric(disp_est, disp_gt, mask, 3.0) for disp_est in disp_ests]

#     if compute_metrics:
#         image_outputs["errormap"] = [disp_error_image_func()(disp_est, disp_gt) for disp_est in disp_ests]

#     return tensor2float(loss), tensor2float(mono_loss), tensor2float(scalar_outputs), image_outputs

def get_row_col(num_pic):
    squr = num_pic ** 0.5
    row = round(squr)
    col = row + 1 if squr - row > 0 else row
    return row, col
 
 
def visualize_feature_map(img_batch):
    feature_map = img_batch.cpu()
    feature_map=feature_map.data.numpy()
    print("fea-heat",feature_map.shape)
 
    feature_map_combination = []
    plt.figure()
 
    num_pic = feature_map.shape[1]
    print("num,pic",num_pic)
    row, col = get_row_col(num_pic)
    print(row,col)
 
    for i in range(0, num_pic):
        feature_map_split = feature_map[0,i, :, :]
        #print(feature_map_split.shape)
        feature_map_combination.append(feature_map_split)
        #plt.subplot(row, col, i + 1)
        plt.imshow(feature_map_split)
        axis('off')
        filename='fea_map/kitti/0/'+str(i+1)+'.png'
        plt.savefig(filename)
    #plt.show()
 
    # 各个特征图按1：1 叠加
    feature_map_sum=feature_map_split[0]
    for i in range(len(feature_map_combination)-1):
        feature_map_sum = feature_map_combination[i+1]+feature_map_sum
    print("!!!!",feature_map_sum.shape)
    plt.imshow(feature_map_sum)
    plt.savefig('fea_map/kitti/0/'+'feature_map_sum.png')

def set_misc(model):
    if args.bn_no_track_stats:
        print("Disabling tracking running stats in batch norm layers")
        model.apply(bn_init_as_tf)

    if args.fix_first_conv_blocks:
        if 'resne' in args.encoder:
            fixing_layers = ['base_model.conv1', 'base_model.layer1.0', 'base_model.layer1.1', '.bn']
        else:
            fixing_layers = ['conv0', 'denseblock1.denselayer1', 'denseblock1.denselayer2', 'norm']
        print("Fixing first two conv blocks")
    elif args.fix_first_conv_block:
        if 'resne' in args.encoder:
            fixing_layers = ['base_model.conv1', 'base_model.layer1.0', '.bn']
        else:
            fixing_layers = ['conv0', 'denseblock1.denselayer1', 'norm']
        print("Fixing first conv block")
    else:
        if 'resne' in args.encoder:
            fixing_layers = ['base_model.conv1', '.bn']
        else:
            fixing_layers = ['conv0', 'norm']
        print("Fixing first conv layer")

    for name, child in model.named_children():
        if not 'encoder' in name:
            continue
        for name2, parameters in child.named_parameters():
            # print(name, name2)
            if any(x in name2 for x in fixing_layers):
                parameters.requires_grad = False





if __name__ == '__main__':
    train()
