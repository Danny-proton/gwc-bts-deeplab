#!/usr/bin/env bash
set -x
DATAPATH="/data/yyx/data/kitti/2015/data_scene_flow"
CUDA_VISIBLE_DEVICES=1,2,5 python -u main_fuzzy.py --dataset kitti \
    --train \
    --train_bio \
    --start_epoch 371 \
    --batch_size 3 \
    --datapath $DATAPATH \
    --trainlist ./filenames/kitti15_train_pseudo.txt \
    --testlist ./filenames/kitti15_val.txt \
    --epochs 1000 --lrepochs "400,600,800:10" \
    --model gwcnet-gc \
    --mono_encoder densenet161_bts \
    --logdir ./checkpoints/kitti_with_fuzzy_loss \
    --mono_model_name bts_eigen_v2_pytorch_densenet161 \
    --loadckpt /data1/dyf2/gwc-deeplab-refined-test/checkpoints/kitti_with_fuzzy_loss/bio_checkpoint_000370.ckpt \
    --sig_resume /data1/dyf2/gwc-deeplab-refined-test/checkpoints/kitti_with_fuzzy_loss/sig_checkpoint_fuzzy_000143.ckpt \
    --mono_checkpoint_path /data1/dyf2/gwc-deeplab-refine/GwcNet-master/checkpoints/mono_kitti/bts_eigen_v2_pytorch_densenet161/model \
    --test_batch_size 1 \
    --mono_max_depth 80 \
    --do_kb_crop \
    --maxdisp 192 \
    #--loadckpt ./checkpoints/kitti/only_update_bn_ft_lr4/checkpoint_000032.ckpt \
    