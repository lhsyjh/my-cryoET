# 修改ConvNeXt适配cryoET
进度：fcmae改成3D医学版本，用10441数据集跑起来了。当前数据集数量太小，而且改得也很粗暴，后面看怎么微调。

## 改动的地方
main_pretrain_cryo.py 作为主代码

修改包括：

-- class Resize3D 目前只是为3D数据做了个简单的适配，性能肯定不是最佳的，只是先能用，3D医学应该有专用的包才对，resize之后应该还有翻转、随机裁剪等功能需要实现

-- class ZarrDataset 同上

-- define the model部分对应改成200维度

-- engine_pretrain.py 在enumerate迭代对象返回只有samples，去掉了lable

-- fcmae.py 99行，def patchify里都改成200维度

运行命令：

python -m torch.distributed.launch --nproc_per_node=1 main_pretrain_cryo.py --model convnextv2_base --batch_size 4 --update_freq 8 --blr 1.5e-4 --epochs 1600 --warmup_epochs 40 --data_path /home/lzyh/cryoET_data/10441/ --output_dir ./out

数据集链接：https://cryoetdataportal.czscience.com/datasets/10441?dataset_id=10441



