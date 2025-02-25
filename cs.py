import zarr
import numpy as np
# import mrcfile
#
# # 打开 mrc 文件
# with mrcfile.open('pp7_vlp-1.0_segmentationmask.mrc', mode='r') as mrc:
#     data = mrc.data  # 获取 3D 数据
#     print(data.shape)  # 打印数据的维度
#



# 打开一个 Zarr 文件
zarr_file = zarr.open('/home/lzyh/cryoET_data/10441/TS_25/Reconstructions/VoxelSpacing10.000/Tomograms/100/TS_25.zarr/', mode='r')

# 查看文件中的所有数据集
print(zarr_file)

# 访问特定的数据集（假设文件中有一个名为 "dataset_name" 的数据集）
dataset = zarr_file['0']

# 打印数据集的形状和数据类型
print(dataset.shape)
print(dataset.dtype)
# 检查是否是 NumPy 数组
if isinstance(dataset[:], np.ndarray):
    print("This dataset is a NumPy array.")
else:
    print("This dataset is not a NumPy array.")