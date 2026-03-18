# astrbot_plugin_moderation

群消息内容审核插件。

## 功能

| 消息类型 | 处理方式 |
|---|---|
| `.apk` 文件 | 规则直接拦截 |
| 视频 | 规则直接拦截 |
| 文本 | NIM Llama Guard（`nvidia/llama-guard-4-12b`）|
| 图片 | OpenAI Moderation（`omni-moderation-latest`，免费）|

违规后依次：撤回消息 → 禁言 N 分钟 → 记录日志

## 安装

```bash
# 在 AstrBot 插件目录下
git clone https://github.com/Nayukiiii/astrbot_plugin_moderation
```

然后在 AstrBot 管理面板重载插件，填写配置项即可。

## 配置项

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `nim_api_key` | NVIDIA NIM API Key | 空 |
| `openai_api_key` | OpenAI API Key | 空 |
| `enabled_groups` | 开启审核的群号列表，空=全部 | `[]` |
| `ban_duration` | 禁言时长（秒） | `600` |

## 指令

| 指令 | 说明 |
|---|---|
| `/modlog [n]` | 查看最近 n 条违规记录（默认10条）|

## 日志

违规记录写入插件目录下的 `moderation_log.jsonl`，每行一条 JSON：

```json
{"time": "2026-03-18T12:00:00", "group_id": "123456", "sender_id": "987654", "msg_type": "image", "reason": "图片违规：sexual"}
```

## 注意

- NIM Llama Guard 有免费额度限速（429），高频群可能偶发跳过
- OpenAI Moderation API 完全免费，无额度限制
- 需要 NapCat 有撤回和禁言权限（bot 需是群管理员）
