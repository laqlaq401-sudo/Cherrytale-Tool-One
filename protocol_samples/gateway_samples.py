"""游戏网关的真实抓包样本（**由 tools/extract_har.py 自动生成，请勿手工编辑**）。

【这批样本解决什么问题】
协议逆向上最容易犯的错，是「按 dump.cs 的字段清单推断出编号，然后直接拿它组包」。
编号一旦差一位，服务端会安静地把值读到别的字段上 —— 现象是「参数看着没错但行为怪」，
而错误信息帮不上任何忙。

所以本项目要求：**任何报文模型都要在真实字节上验证过**。
本文件就是那批真实字节：请求侧一条（首次握手），响应侧多条（覆盖多种业务包）。

【怎么用】
    from protocol_samples.gateway_samples import SAMPLES, VERSION_CONTROL_REQUEST_HEX

    sample = SAMPLES[0]
    bytes.fromhex(sample["response_hex"])      # 真实响应字节
    sample["response_packet_id"]               # 已经预先解出的消息号（省一步排查）

【数据来源与已知局限】
- 响应：HAR 里的 ``content.text``（base64，**无损**）；
- 请求：只有首次握手那条 —— 由用户从 Fiddler 的 HexView 复制。
  HAR 里的请求体是 text，二进制字节被替换成了 ``\ufffd``，**无法还原**，
  因此其余样本的 ``request_hex`` 是空串（脚本刻意保留字段以标注这种缺失）。
- 大响应（超过 4096 字节）只内联前 256 字节，
  ``response_truncated`` 为 True；完整数据仍在 captures/ 的 HAR 里。
"""

from __future__ import annotations

from typing import Any, Final

#: 首次握手请求（88 字节）—— 整条链路的第一个包，也是唯一有完整请求体的样本
VERSION_CONTROL_REQUEST_HEX: Final[str] = (
    "08c785061a022d3150ffffffffffffffffff016089e7ddee8b34722042413332"
    "45373841334335323634333338313034424443413637444136393933baac3018"
    "0802120e322e322e302d656c2d682d77696e1a047a68636e"
)

#: 抓包样本（每条 = 一次 POST 的请求/响应，以及预先解出的消息号与包名）
SAMPLES: Final[tuple[dict[str, Any], ...]] = (
    {
        'name': 'login#1',
        'source': 'captures/login.har',
        'entry_index': 1,
        'request_size': 88,
        'request_hex': (
            "08c785061a022d3150ffffffffffffffffff016089e7ddee8b34722042413332"
            "45373841334335323634333338313034424443413637444136393933baac3018"
            "0802120e322e322e302d656c2d682d77696e1a047a68636e"
        ),
        'request_hex_source': '用户从 Fiddler HexView 复制（时间戳/vCode 随抓包不同）',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '88'},
        'response_size': 650,
        'response_hex': (
            "08c885068201440a203244423942393832434136393032373630363142393045"
            "3633463343334433381220344537333536314537393737394346323931324443"
            "304246373238423331463992ac30140801080108010801080108010801080108"
            "010800c2ac30a2040800121667616d652d63742d6c6162732e65636368692e78"
            "787818e00e220fe7b3bbe7bb9fe7bbb4e68aa4e4b8ad280132d1027b22557365"
            "48747470466163746f7279223a2230222c22505645566572696679223a223122"
            "2c225775446f75505650223a2231222c22646e735f67726f7570223a2233222c"
            "2259696d6f426174746c65223a2231222c22646f776e6c6f6164223a22687474"
            "70733a2f2f6c2e6879656e61646174612e636f6d2f732f32373049756c222c22"
            "45726f6c61627350726f6d6f74696f6e55726c223a2268747470733a2f2f7777"
            "772e65726f2d6c6162732e636f6d2f70726f66696c652d70726f6d6f74652e68"
            "746d6c222c22636f6e6e656374457272223a2231222c2262616e6e65724d6435"
            "223a226663316531323461336137613031323465656231656461353937626335"
            "303039222c2263646e5f67726f7570223a2231222c2250565056657269667922"
            "3a2231222c224e6f746966636174696f6e223a2230222c22616e616c79736973"
            "537461747573223a2231227d3a20643439373035353236313065616439623038"
            "6165303131343039623165336630421667616d652d63742d6c6162732e656363"
            "68692e78787848e50e521a68747470733a2f2f706664642e38386b6f6e677175"
            "652e636f6d521f68747470733a2f2f70617463682d63742d6c6162732e656363"
            "68692e7878785a07382e382e382e38621667616d652d63742d6c6162732e6563"
            "6368692e78787868ec0e"
        ),
        'response_truncated': False,
        'response_packet_id': 99016,
        'response_class': 'VersionControlServerRes',
    },
    {
        'name': 'cherrytale-fiddler-1#24',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 24,
        'request_size': 88,
        'request_hex': (
            "08c785061a022d3150ffffffffffffffffff016089e7ddee8b34722042413332"
            "45373841334335323634333338313034424443413637444136393933baac3018"
            "0802120e322e322e302d656c2d682d77696e1a047a68636e"
        ),
        'request_hex_source': '用户从 Fiddler HexView 复制（时间戳/vCode 随抓包不同）',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '88'},
        'response_size': 650,
        'response_hex': (
            "08c885068201440a203244423942393832434136393032373630363142393045"
            "3633463343334433381220344537333536314537393737394346323931324443"
            "304246373238423331463992ac30140801080108010801080108010801080108"
            "010800c2ac30a2040800121667616d652d63742d6c6162732e65636368692e78"
            "787818e00e220fe7b3bbe7bb9fe7bbb4e68aa4e4b8ad280132d1027b22557365"
            "48747470466163746f7279223a2230222c22505645566572696679223a223122"
            "2c225775446f75505650223a2231222c22646e735f67726f7570223a2233222c"
            "2259696d6f426174746c65223a2231222c22646f776e6c6f6164223a22687474"
            "70733a2f2f6c2e6879656e61646174612e636f6d2f732f32373049756c222c22"
            "45726f6c61627350726f6d6f74696f6e55726c223a2268747470733a2f2f7777"
            "772e65726f2d6c6162732e636f6d2f70726f66696c652d70726f6d6f74652e68"
            "746d6c222c22636f6e6e656374457272223a2231222c2262616e6e65724d6435"
            "223a226663316531323461336137613031323465656231656461353937626335"
            "303039222c2263646e5f67726f7570223a2231222c2250565056657269667922"
            "3a2231222c224e6f746966636174696f6e223a2230222c22616e616c79736973"
            "537461747573223a2231227d3a20643439373035353236313065616439623038"
            "6165303131343039623165336630421667616d652d63742d6c6162732e656363"
            "68692e78787848e50e521a68747470733a2f2f706664642e38386b6f6e677175"
            "652e636f6d521f68747470733a2f2f70617463682d63742d6c6162732e656363"
            "68692e7878785a07382e382e382e38621667616d652d63742d6c6162732e6563"
            "6368692e78787868ec0e"
        ),
        'response_truncated': False,
        'response_packet_id': 99016,
        'response_class': 'VersionControlServerRes',
    },
    {
        'name': 'cherrytale-fiddler-1#47',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 47,
        'request_size': 330,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '330'},
        'response_size': 8872,
        'response_hex': (
            "08ea078201440a20324442394239383243413639303237363036314239304536"
            "3346334333443338122034453733353631453739373739434632393132444330"
            "42463732384233314639d23ecd440800108de9fd021a2e0801120ce4bb99e5ba"
            "a6e7919ee68b8918002212e6ada1e8bf8ee4be86e588b0e6abbbe5a28328ecfb"
            "0f320038011a2808021206e5b88ce788be18002212e6ada1e8bf8ee4be86e588"
            "b0e6abbbe5a28328dbe703320038011a2b08031209e6a183e6a882e7b5b21800"
            "2212e6ada1e8bf8ee4be86e588b0e6abbbe5a28328dafa02320038011a2e0804"
            "120ce4ba9ee6a3aee7be85e8988b18002212e6ada1e8bf8ee4be86e588b0e6ab"
        ),
        'response_truncated': True,
        'response_packet_id': 1002,
        'response_class': 'AccountClientInfoLoginRes',
    },
    {
        'name': 'cherrytale-fiddler-1#51',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 51,
        'request_size': 105,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '105'},
        'response_size': 0,
        'response_hex': '',
        'response_truncated': False,
        'response_packet_id': None,
        'response_class': None,
    },
    {
        'name': 'cherrytale-fiddler-1#108',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 108,
        'request_size': 178,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '178'},
        'response_size': 0,
        'response_hex': '',
        'response_truncated': False,
        'response_packet_id': None,
        'response_class': None,
    },
    {
        'name': 'cherrytale-fiddler-1#111',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 111,
        'request_size': 192,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '192'},
        'response_size': 24646,
        'response_hex': (
            "08f2075a130a0c08ddf1c078108284af5f1847106418c7038201440a20324442"
            "3942393832434136393032373630363142393045363346334333443338122034"
            "4537333536314537393737394346323931324443304246373238423331463992"
            "3fe2bf01080012bc02088cfabeb00310e8f481b0031a0a088cfabeb003100120"
            "001a0a088cfabeb003100220001a0a088cfabeb003100320001a0a088cfabeb0"
            "03100420001a0a088cfabeb003100520001a0a088cfabeb003100620001a0a08"
            "8cfabeb003100720001a0a088cfabeb003100820001a0a088cfabeb003100920"
            "001a0a088cfabeb003100a20001a0a088cfabeb003100b20001a0a088cfabeb0"
        ),
        'response_truncated': True,
        'response_packet_id': 1010,
        'response_class': 'LoginOtherDataPacketRes',
    },
    {
        'name': 'cherrytale-fiddler-1#113',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 113,
        'request_size': 203,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '203'},
        'response_size': 0,
        'response_hex': '',
        'response_truncated': False,
        'response_packet_id': None,
        'response_class': None,
    },
    {
        'name': 'cherrytale-fiddler-1#115',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 115,
        'request_size': 135,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '135'},
        'response_size': 551,
        'response_hex': (
            "089cf2015a130a0c08ddf1c078108284af5f1847106418c7038201440a203244"
            "4239423938324341363930323736303631423930453633463343334433381220"
            "3445373335363145373937373943463239313244433042463732384233314639"
            "e2910fc20308a1b2b7a80308a2b2b7a80308a3b2b7a80308a4b2b7a80308a5b2"
            "b7a80308a6b2b7a80308a8b2b7a80308a9b2b7a80308aab2b7a80308abb2b7a8"
            "0308acb2b7a80308adb2b7a80308aeb2b7a80308afb2b7a80308b0b2b7a80308"
            "b1b2b7a80308b5b2b7a80308b7b2b7a80308b9b2b7a80308bab2b7a80308c0b2"
            "b7a80308c4b2b7a80308c6b2b7a80308c7b2b7a80308c8b2b7a80308cbb2b7a8"
            "0308cdb2b7a80308d2b2b7a80308d4b2b7a80308d5b2b7a80308d6b2b7a80308"
            "d7b2b7a80308d8b2b7a80308d9b2b7a80308dab2b7a80308dbb2b7a80308dcb2"
            "b7a80308ddb2b7a80308deb2b7a80308dfb2b7a80308e1b2b7a80308e2b2b7a8"
            "0308e3b2b7a80308e4b2b7a80308e5b2b7a80308e6b2b7a80308e7b2b7a80308"
            "e8b2b7a80308e9b2b7a80308eab2b7a80308ebb2b7a80308ecb2b7a80308edb2"
            "b7a80308eeb2b7a80308efb2b7a80308f1b2b7a80308f2b2b7a80308f3b2b7a8"
            "0308f4b2b7a80308f5b2b7a80308f6b2b7a80308f7b2b7a80308fab2b7a80308"
            "fbb2b7a80308fcb2b7a80308fdb2b7a8030880b3b7a8030881b3b7a8030884b3"
            "b7a8030885b3b7a8030886b3b7a8030887b3b7a8030888b3b7a80308c9f8b7a8"
            "0310a1edb1a903"
        ),
        'response_truncated': False,
        'response_packet_id': 31004,
        'response_class': 'TeachingRecordRes',
    },
    {
        'name': 'cherrytale-fiddler-1#118',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 118,
        'request_size': 1739,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '1739'},
        'response_size': 73,
        'response_hex': (
            "08008201440a2032444239423938324341363930323736303631423930453633"
            "4633433344333812203445373335363145373937373943463239313244433042"
            "463732384233314639"
        ),
        'response_truncated': False,
        'response_packet_id': 0,
        'response_class': None,
    },
    {
        'name': 'cherrytale-fiddler-1#121',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 121,
        'request_size': 135,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '135'},
        'response_size': 775,
        'response_hex': (
            "08a082025a130a0c08ddf1c078108284af5f1847106418c7038201440a203244"
            "4239423938324341363930323736303631423930453633463343334433381220"
            "3445373335363145373937373943463239313244433042463732384233314639"
            "829210a2050ad5010891acb8a102108085d3dc83341880a59bde8c342212e7b1"
            "b3e5a9ade799bbe585a5e78d8ee58bb52a12e7b1b3e5a9ade799bbe585a5e78d"
            "8ee58bb5321ee4b883e5a4a9e799bbe585a5e58fafe9a098e58f96e78d8ee58b"
            "b5e380823a124c6f67696e5f5265776172645f42475f30314209080110aac8cd"
            "5f1802420a0802108184af5f18f403420b080310b284af5f18a0c21e42090804"
            "10c3d3d35f180a420a080510ef84af5f18f40342090806108a84af5f180a4209"
            "0807109d85af5f1805481950025a0e63743030385f43675f5370696e650add01"
            "0892acb8a10210808d85fd85341880a59bde8c342218e698a5e5aeb5e5a49ce5"
            "aeb4e799bbe585a5e78d8ee58bb52a18e698a5e5aeb5e5a49ce5aeb4e799bbe5"
            "85a5e78d8ee58bb5321ee4b883e5a4a9e799bbe585a5e58fafe9a098e58f96e7"
            "8d8ee58bb5e380823a124c6f67696e5f5265776172645f42475f303142090801"
            "10b085af5f18324209080210b085af5f18324209080310b085af5f1832420908"
            "0410b085af5f18324209080510b085af5f18324209080610b085af5f18324209"
            "080710b085af5f1832481250025a0e63743038365f43675f5370696e650ae701"
            "0893acb8a10210809de9bd8a341880bdb1bf9334221be99693e5aeaee9babbe7"
            "9086e7b9aae799bbe585a5e78d8ee58bb52a1be99693e5aeaee9babbe79086e7"
            "b9aae799bbe585a5e78d8ee58bb5321ee4b883e5a4a9e799bbe585a5e58fafe9"
            "a098e58f96e78d8ee58bb5e380823a124c6f67696e5f5265776172645f42475f"
            "30314209080110adc8cd5f18014209080210c3d3d35f180a420a0803108184af"
            "5f18ac02420b080410b284af5f18c0843d420a080510ef84af5f18f403420908"
            "06108a84af5f180a4209080710a984af5f1801480450025a0e63743038375f43"
            "675f5370696e65"
        ),
        'response_truncated': False,
        'response_packet_id': 33056,
        'response_class': 'GetActivityLoginRewardsRes',
    },
    {
        'name': 'cherrytale-fiddler-1#123',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 123,
        'request_size': 135,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '135'},
        'response_size': 146,
        'response_hex': (
            "0886fa015a130a0c08ddf1c078108284af5f1847106418c7038201440a203244"
            "4239423938324341363930323736303631423930453633463343334433381220"
            "3445373335363145373937373943463239313244433042463732384233314639"
            "b2d00f2e0a0e08001001100210031802200728010a0c08011001100218002000"
            "28010a0e080210021802180f200720632801"
        ),
        'response_truncated': False,
        'response_packet_id': 32006,
        'response_class': 'GetHomeBannerPopUpSettingsRes',
    },
    {
        'name': 'cherrytale-fiddler-1#127',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 127,
        'request_size': 134,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '134'},
        'response_size': 13597,
        'response_hex': (
            "0892565a130a0c08ddf1c078108284af5f1847106418c7038201440a20324442"
            "3942393832434136393032373630363142393045363346334333443338122034"
            "4537333536314537393737394346323931324443304246373238423331463992"
            "b105b9690aac0108a78e853d10001a2108ffffffffffffffffff0110ffffffff"
            "ffffffffff0118ffffffffffffffffff0122274c6576656c5f41637469766974"
            "795f42616e6e65725f4441524b5f506f7274616c5f502e706e67288095b79d88"
            "343080a59bde8c343a2b4c6576656c5f41637469766974795f4d61696e42616e"
            "6e65725f4441524b5f506f7274616c5f502e706e67400f48014801480150ffff"
        ),
        'response_truncated': True,
        'response_packet_id': 11026,
        'response_class': 'GetAllActivityStageRes',
    },
    {
        'name': 'cherrytale-fiddler-1#128',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 128,
        'request_size': 135,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '135'},
        'response_size': 118,
        'response_hex': (
            "08d085068201440a203244423942393832434136393032373630363142393045"
            "3633463343334433381220344537333536314537393737394346323931324443"
            "304246373238423331463982ad302708ffffffffffffffffff01121808231001"
            "188085d3dc83342080a59bde8c342882bfdf80011801"
        ),
        'response_truncated': False,
        'response_packet_id': 99024,
        'response_class': 'SyncDataRes',
    },
    {
        'name': 'cherrytale-fiddler-1#130',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 130,
        'request_size': 135,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '135'},
        'response_size': 145,
        'response_hex': (
            "08a2a4015a130a0c08ddf1c078108284af5f1847106418c7038201440a203244"
            "4239423938324341363930323736303631423930453633463343334433381220"
            "3445373335363145373937373943463239313244433042463732384233314639"
            "92a20a2d08001096d4011a0ce5a49ce889b2e5b09ae6b5852087b3f6ca02280a"
            "30c3f8e5be0338ffffffffffffffffff01"
        ),
        'response_truncated': False,
        'response_packet_id': 21026,
        'response_class': 'AllianceDataToOtherSysRes',
    },
    {
        'name': 'cherrytale-fiddler-1#132',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 132,
        'request_size': 143,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '143'},
        'response_size': 4356,
        'response_hex': (
            "08fcd2015a130a0c08ddf1c078108284af5f1847106418c7038201440a203244"
            "4239423938324341363930323736303631423930453633463343334433381220"
            "3445373335363145373937373943463239313244433042463732384233314639"
            "e2970d9f210a8301080710051a03373030208184af5f280030003a1e61756572"
            "2e616e64726f69642e43686572727954616c655f47726f77746840004a033330"
            "30500058ffffffffffffffffff0160ac026a022d317000780082010237308a01"
            "0237379001c2039a0103373030a2010fe8b2b4e8b393e68890e995b7e7a6aea8"
            "0100b2010608001000180010012a02080032e0050a22080b120de8aea2e99885"
        ),
        'response_truncated': True,
        'response_packet_id': 27004,
        'response_class': 'CashItemListRes',
    },
    {
        'name': 'cherrytale-fiddler-1#134',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 134,
        'request_size': 143,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '143'},
        'response_size': 0,
        'response_hex': '',
        'response_truncated': False,
        'response_packet_id': None,
        'response_class': None,
    },
    {
        'name': 'cherrytale-fiddler-1#137',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 137,
        'request_size': 134,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '134'},
        'response_size': 565,
        'response_hex': (
            "0884565a130a0c08ddf1c078108284af5f1847106418c7038201440a20324442"
            "3942393832434136393032373630363142393045363346334333443338122034"
            "45373335363145373937373943463239313244433042463732384233314639a2"
            "b005d103080008000800124f08f6ced1301000180018001800200028ffffffff"
            "ffffffffff0130feffffffffffffffff01380042090800108184af5f181e48ff"
            "ffffffffffffffff0150ffffffffffffffffff01580060c2cf8e31123d08e0cd"
            "d13010021801180118002000280130feffffffffffffffff0138004209080010"
            "8184af5f181e48ffffffffffffffffff015000580060c1cf8e31123d08e1cdd1"
            "3010021801180118002000280130feffffffffffffffff013800420908001081"
            "84af5f181e48ffffffffffffffffff015000580060c1cf8e31123d08e2cdd130"
            "10021801180118012000280130feffffffffffffffff01380042090800108184"
            "af5f181e48ffffffffffffffffff015000580060c1cf8e31123d08e3cdd13010"
            "021801180118012000280130feffffffffffffffff01380042090800108184af"
            "5f181e48ffffffffffffffffff015000580060c1cf8e31123d08e4cdd1301002"
            "1801180118012000280130feffffffffffffffff01380042090800108184af5f"
            "181e48ffffffffffffffffff015000580060c1cf8e31123d08e5cdd130100218"
            "01180118012000280130feffffffffffffffff01380042090800108184af5f18"
            "1e48ffffffffffffffffff015000580060c1cf8e31"
        ),
        'response_truncated': False,
        'response_packet_id': 11012,
        'response_class': 'GetLastSectionRes',
    },
    {
        'name': 'cherrytale-fiddler-1#139',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 139,
        'request_size': 134,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '134'},
        'response_size': 694,
        'response_hex': (
            "08bc6d5a130a0c08ddf1c078108284af5f1847106418c7038201440a20324442"
            "3942393832434136393032373630363142393045363346334333443338122034"
            "45373335363145373937373943463239313244433042463732384233314639e2"
            "eb06d2040a130881ffe3bd0110ffffffffffffffffff0118000a130882ffe3bd"
            "0110ffffffffffffffffff0118000a130883ffe3bd0110ffffffffffffffffff"
            "0118000a130884ffe3bd0110ffffffffffffffffff0118000a130885ffe3bd01"
            "10ffffffffffffffffff0118000a130887ffe3bd0110ffffffffffffffffff01"
            "18000a130888ffe3bd0110ffffffffffffffffff0118000a130889ffe3bd0110"
            "ffffffffffffffffff0118000a13088affe3bd0110ffffffffffffffffff0118"
            "000a13088dffe3bd0110ffffffffffffffffff0118000a13088fffe3bd0110ff"
            "ffffffffffffffff0118000a130890ffe3bd0110ffffffffffffffffff011800"
            "0a1308f482e4bd0110ffffffffffffffffff0118000a1308f582e4bd0110ffff"
            "ffffffffffffff0118000a1308f682e4bd0110ffffffffffffffffff0118000a"
            "1308f882e4bd0110ffffffffffffffffff0118000a1308fa82e4bd0110ffffff"
            "ffffffffffff0118000a1308fd82e4bd0110ffffffffffffffffff0118000a13"
            "08fe82e4bd0110ffffffffffffffffff0118000a1308ff82e4bd0110ffffffff"
            "ffffffffff0118000a13088083e4bd0110ffffffffffffffffff0118000a0f08"
            "fcb5e4bd011080a59bde8c3418000a0f08fdb5e4bd011080a59bde8c3418000a"
            "0f08feb5e4bd011080bdb1bf933418000a0f08ffb5e4bd011080bdb1bf933418"
            "000a0f08c487e4bd011080a59bde8c3418000a0f08bb9ee4bd011080a59bde8c"
            "3418000a0f08c39ee4bd011080bdb1bf933418000a0f0896a6e4bd011080bdb1"
            "bf933418000a0f08df8ee4bd011080a59bde8c341800"
        ),
        'response_truncated': False,
        'response_packet_id': 14012,
        'response_class': 'GetStoreTypeListRes',
    },
    {
        'name': 'cherrytale-fiddler-1#140',
        'source': 'captures/cherrytale-fiddler-1.har',
        'entry_index': 140,
        'request_size': 135,
        'request_hex': '',
        'request_hex_source': '缺失：HAR 导出的请求体是有损文本，需 HexView 提供',
        'request_headers': {'Host': 'game-ct-labs.ecchi.xxx:1893', 'User-Agent': 'UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)', 'Accept': '*/*', 'Accept-Encoding': 'deflate, gzip', 'Content-Type': 'application/octet-stream', 'X-Unity-Version': '2020.3.49f1', 'Content-Length': '135'},
        'response_size': 118,
        'response_hex': (
            "08d085068201440a203244423942393832434136393032373630363142393045"
            "3633463343334433381220344537333536314537393737394346323931324443"
            "304246373238423331463982ad302708ffffffffffffffffff01121808231001"
            "188085d3dc83342080a59bde8c342882bfdf80011801"
        ),
        'response_truncated': False,
        'response_packet_id': 99024,
        'response_class': 'SyncDataRes',
    },
)
