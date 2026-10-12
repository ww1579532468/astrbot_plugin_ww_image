# WW 画图

AstrBot 文生图、图生图插件，提供 `ww_image` AI 工具。
工具立即返回，图片在后台生成后直接发给用户。

## 配置

- `base_url`：OpenAI 兼容绘图接口地址。
- `api_key`：绘图接口密钥，只在部署配置中填写。
- `model`：绘图模型名。
- `timeout`：请求超时秒数，默认 `120`。

依赖见 `requirements.txt`，要求 AstrBot `>=4.16,<5`。

## 工具参数

- `prompt`：图片描述。
- `refs`：参考图片，支持 URL、data URL 或本地路径，多图用换行或 JSON 数组。
- `aspect_ratio`：宽高比例。
- `size`：图片尺寸。
- `extra_params`：额外参数，格式为 `--key value`。

具体参数支持取决于绘图接口和模型。
插件尝试发送简短结果说明，并将画图结果记录到原对话历史。
使用本地参考图片时，只提供自己有权使用的图片路径。
请勿提交密钥、部署配置、历史备份或生成图片。

## 许可证

本项目采用 Creative Commons Attribution 4.0 International（CC BY 4.0）协议。
完整条款见 `LICENSE`。署名作者：ww。
