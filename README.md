# WW 画图

支持多模型的文生图、图生图插件。

## 配置

- `base_url`：OpenAI 兼容绘图接口地址。
- `api_key`：绘图接口密钥。
- `models`：绘图模型列表，一行一个模型，第一个有效模型为默认模型。
- `timeout`：请求超时秒数。

## 工具参数

- `prompt`：图片描述。
- `model`：可选，直接指定本次使用的模型。
- `refs`：参考图片。
- `aspect_ratio`：宽高比例。
- `size`：图片尺寸。
- `extra_params`：额外参数。
