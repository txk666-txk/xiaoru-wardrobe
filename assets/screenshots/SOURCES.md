# 截图素材来源

本目录的 `storefront.jpg`、`styling-chat.jpg` 和 `virtual-tryon.jpg` 用于说明项目界面与功能。它们是界面截图及模型生成示例，不是实际顾客、真实交易或真实试穿的记录。截图中的价格、库存、尺码和订单均为演示数据。

## 界面与助手头像

- 页面为本项目界面，代码来源及 GPL-3.0 说明见[根 README](../../README.md)与[许可证](../../LICENSE)。
- `storefront.jpg` 和 `styling-chat.jpg` 由项目作者提供，分别展示本机商城 / Live2D 客服工作台、照片穿搭上传入口；原始截图仅转换为 JPEG，未重绘界面或替换其中的服装与角色。
- `storefront.jpg` 包含本机配置的 Live2D 角色 `beierfasite_2`、配套头像和第三方商品参考照片，保留画面中原有标识。这些素材的权利归原权利人；截图不表示店铺合作、素材为原创或另行授予角色 / 商品图许可。角色运行库、模型、贴图、动作文件和完整本机商品图库不随仓库打包。
- 新克隆未配置角色时使用自制的 [xiaoru-avatar.svg](../../gui/static/images/xiaoru-avatar.svg)。两张本机截图不代表干净克隆会自动带有相同角色和完整商品目录。第二张仅展示上传入口，不是视觉分析结果或准确率证据。
- `virtual-tryon.jpg` 中的服装照片使用下述开放许可小样，人物输入来自 FASHN 官方示例；截图不包含用户私人上传照片。

## 开放许可服装照片

仓库自带服装小样的完整逐图元数据、原图链接、下载版本、文件校验值与改动说明见 [public_fashion_sources.json](../../agent/data/public_fashion_sources.json)。这些记录适用于公开小样及试衣示例，不适用于前两张截图中的本机第三方商品目录。试衣截图仅将输入照片缩放并置于页面中；本地服装图片的裁切、缩放与 JPEG 转换已分别记录。照片保持各自许可，不因出现在截图中而统一改标为 GPL。

- **gray-tee**：[File:T-shirt mocup gray.jpg](<https://commons.wikimedia.org/wiki/File:T-shirt_mocup_gray.jpg>)；作者/来源署名：EdaBojci；[CC0](https://creativecommons.org/publicdomain/zero/1.0/deed.en)。
- **black-jeans**：[File:Jeans BW 2 (3213391837).jpg](<https://commons.wikimedia.org/wiki/File:Jeans_BW_2_(3213391837).jpg>)；作者/来源署名：THOR；[CC BY 2.0](https://creativecommons.org/licenses/by/2.0)。
- **white-longsleeve**：[File:T-shirt mocup white.jpg](<https://commons.wikimedia.org/wiki/File:T-shirt_mocup_white.jpg>)；作者/来源署名：EdaBojci；[CC0](https://creativecommons.org/publicdomain/zero/1.0/deed.en)。
- **ivory-shirt**：[File:Dress shirt MET 2011.46 F.jpeg](<https://commons.wikimedia.org/wiki/File:Dress_shirt_MET_2011.46_F.jpeg>)；作者/来源署名：Lanvin（来源 Artist 字段所列服装设计者/制造者；不是对摄影者身份的推定）；馆方归属：The Metropolitan Museum of Art；[CC0](https://creativecommons.org/publicdomain/zero/1.0/deed.en)。
- **orange-shirt**：[File:Shirt, men's (AM 2015.44.1-1).jpg](<https://commons.wikimedia.org/wiki/File:Shirt,_men%27s_(AM_2015.44.1-1).jpg>)；作者/来源署名：Manhattan（来源 Artist 字段所列服装设计者/制造者；不是对摄影者身份的推定）；馆方归属：Auckland War Memorial Museum Tāmaki Paenga Hira；[CC BY 4.0](https://creativecommons.org/licenses/by/4.0)。
- **striped-tee**：[File:Blouse (AM 2015.51.2-1).jpg](<https://commons.wikimedia.org/wiki/File:Blouse_(AM_2015.51.2-1).jpg>)；作者/来源署名：Unknown author Unknown author；馆方归属：Auckland War Memorial Museum Tāmaki Paenga Hira；[CC BY 4.0](https://creativecommons.org/licenses/by/4.0)。
- **floral-shirt**：[File:Shirt, man's (AM 2016.80.1-4).jpg](<https://commons.wikimedia.org/wiki/File:Shirt,_man%27s_(AM_2016.80.1-4).jpg>)；作者/来源署名：Clacton and Frinton（来源 Artist 字段所列服装设计者/制造者；不是对摄影者身份的推定）；馆方归属：Auckland War Memorial Museum Tāmaki Paenga Hira；[CC BY 4.0](https://creativecommons.org/licenses/by/4.0)。
- **black-dress**：[File:Black silk dress by Emma Knuckey.jpg](<https://commons.wikimedia.org/wiki/File:Black_silk_dress_by_Emma_Knuckey.jpg>)；作者/来源署名：Museum photographer, dress by Emma Knuckey；馆方归属：Auckland War Memorial Museum Tāmaki Paenga Hira；[CC BY 4.0](https://creativecommons.org/licenses/by/4.0)。

CC BY 照片的作者、来源与许可链接随本说明和逐图清单一并保留；其中灰、白上衣的底部背景与拍摄者脚部已裁掉，未改变衣物颜色。其他照片按清单进行缩放和格式转换。

## FASHN 官方示例与生成结果

`virtual-tryon.jpg` 的示例人物输入采用 FASHN 官方仓库跟踪的 `examples/data/garment.webp`（文件名为 garment，画面包含黑色上衣男模），再由本项目调用 FASHN VTON v1.5 生成试衣结果并显示于页面。服装输入来自上方有来源记录的开放许可小样。人物输入经过解码/尺寸规范化以供模型调用；输出改变了人物画面中的服装，是模型生成内容，不是该人物实际穿着或认可该商品的证明。

- 上游项目：[fashn-AI/fashn-vton-1.5](https://github.com/fashn-AI/fashn-vton-1.5)，核对版本 `7c0f10af3f91ad4048fe9729c470a13ef905d25a`。
- 本截图的人物输入：[examples/data/garment.webp](https://github.com/fashn-AI/fashn-vton-1.5/blob/7c0f10af3f91ad4048fe9729c470a13ef905d25a/examples/data/garment.webp)。上游另有被跟踪的 [examples/data/model.webp](https://github.com/fashn-AI/fashn-vton-1.5/blob/7c0f10af3f91ad4048fe9729c470a13ef905d25a/examples/data/model.webp) 示例。
- 上游署名：**Copyright 2025 FASHN AI**。
- 上游仓库 [LICENSE](https://github.com/fashn-AI/fashn-vton-1.5/blob/7c0f10af3f91ad4048fe9729c470a13ef905d25a/LICENSE)、README 与包元数据标示 **Apache-2.0**；本目录保留[上游许可证全文](LICENSE-FASHN-Apache-2.0.txt)。

此次对上游版本的检查未找到单独的 NOTICE、示例图片许可排除说明或人物肖像授权文件。因此这里只记录仓库公布的许可与文件来源，不声明已获得独立人物肖像授权，也不表示人物或 FASHN AI 为本项目背书。截图中的合成结果不保证服装细节和真实合身程度。
