# 网关流量清单

> 本文件由 `tools/extract_har.py` 自动生成，请勿手工编辑。
> 重新生成：`python tools/extract_har.py`

## 这批数据回答了什么

| 问题 | 结论 |
|---|---|
| 网关地址与端口 | `game-ct-labs.ecchi.xxx:1893`（服务端在载荷中也确认了它） |
| 通信方式 | 标准 HTTP POST 一问一答，`Content-Type: application/octet-stream` |
| 报文格式 | 裸 protobuf（`RootPacket`：字段 1 = `packetID`） |
| 签名 / 加密 / 分帧 | **都没有** —— 无需反汇编去挖 vCode 算法 |
| 包裹层 | 无私有长度前缀；HTTP 自带 `Content-Length` |

## 请求头（抓包实测，逐项照抄）

```
Host: game-ct-labs.ecchi.xxx:1893
User-Agent: UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)
Accept: */*
Accept-Encoding: deflate, gzip
Content-Type: application/octet-stream
X-Unity-Version: 2020.3.49f1
Content-Length: 88
```

## 流量明细

| # | 来源 | 请求字节 | 响应字节 | 响应消息号 | 响应包名 | 请求体 hex |
|---:|---|---:|---:|---:|---|---|
| 1 | login#1 | 88 | 650 | 99016 | VersionControlServerRes | 有 |
| 2 | cherrytale-fiddler-1#24 | 88 | 650 | 99016 | VersionControlServerRes | 有 |
| 3 | cherrytale-fiddler-1#47 | 330 | 8872 | 1002 | AccountClientInfoLoginRes | 缺失 |
| 4 | cherrytale-fiddler-1#51 | 105 | 0 | ? | （外层结构待确认） | 缺失 |
| 5 | cherrytale-fiddler-1#108 | 178 | 0 | ? | （外层结构待确认） | 缺失 |
| 6 | cherrytale-fiddler-1#111 | 192 | 24646 | 1010 | LoginOtherDataPacketRes | 缺失 |
| 7 | cherrytale-fiddler-1#113 | 203 | 0 | ? | （外层结构待确认） | 缺失 |
| 8 | cherrytale-fiddler-1#115 | 135 | 551 | 31004 | TeachingRecordRes | 缺失 |
| 9 | cherrytale-fiddler-1#118 | 1739 | 73 | ? | （外层结构待确认） | 缺失 |
| 10 | cherrytale-fiddler-1#121 | 135 | 775 | 33056 | GetActivityLoginRewardsRes | 缺失 |
| 11 | cherrytale-fiddler-1#123 | 135 | 146 | 32006 | GetHomeBannerPopUpSettingsRes | 缺失 |
| 12 | cherrytale-fiddler-1#125 | 135 | 3521 | 33058 | GetLoginPassRes | 缺失 |
| 13 | cherrytale-fiddler-1#127 | 134 | 13597 | 11026 | GetAllActivityStageRes | 缺失 |
| 14 | cherrytale-fiddler-1#128 | 135 | 118 | 99024 | SyncDataRes | 缺失 |
| 15 | cherrytale-fiddler-1#130 | 135 | 145 | 21026 | AllianceDataToOtherSysRes | 缺失 |
| 16 | cherrytale-fiddler-1#132 | 143 | 4356 | 27004 | CashItemListRes | 缺失 |
| 17 | cherrytale-fiddler-1#134 | 143 | 0 | ? | （外层结构待确认） | 缺失 |
| 18 | cherrytale-fiddler-1#137 | 134 | 565 | 11012 | GetLastSectionRes | 缺失 |
| 19 | cherrytale-fiddler-1#139 | 134 | 694 | 14012 | GetStoreTypeListRes | 缺失 |
| 20 | cherrytale-fiddler-1#140 | 135 | 118 | 99024 | SyncDataRes | 缺失 |

## 统计

- 抓到的网关 POST 总数：**20**
- 能解出响应消息号的：**15**
- 请求体缺失的：**18**（HAR 导出的请求体是有损文本；如需完整字节，请在 Fiddler 里用 HexView 复制）
