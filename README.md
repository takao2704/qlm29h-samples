# QLM29H Samples

Quectel QLM29HBAA-GM 用の RTK（NTRIP）サンプルスクリプト集です。

## ファイル構成

| ファイル | 説明 |
|---|---|
| [rtk_client.py](rtk_client.py) | NTRIPクライアント。RTCM補正をモジュールに転送し、Fix状態をログ表示する基本サンプル |
| [rtk_harvest.py](rtk_harvest.py) | `rtk_client.py` に加え、GNSS位置情報を SORACOM Harvest Data（Unified Endpoint）へ定期送信するサンプル |
| [rtk_nmea_unified.py](rtk_nmea_unified.py) | NTRIP補正、NMEA全文の構造化、Unified Endpointへのスプール付き送信を行うデーモン向けサンプル |

## ドキュメント

| ファイル | 説明 |
|---|---|
| [docs/sequence.md](docs/sequence.md) | 3つのPythonスクリプトの実行シーケンス |

## セットアップ

```bash
pip3 install -r requirements.txt
```

NTRIP の認証情報は環境変数で渡してください（SORACOMから提供されます）。

```bash
export NTRIP_USER="..."
export NTRIP_PASS="..."
```

## 実行方法

```bash
python3 rtk_client.py
# または
python3 rtk_harvest.py
```

シリアルポートやNTRIP接続先などは各スクリプト先頭の設定セクションを編集してください。

### `rtk_nmea_unified.py` のspool先

`rtk_nmea_unified.py` はUnified Endpoint送信用payloadを一度spoolしてから送信します。
SDカードへの継続書き込みを避けたい場合は、RAM上のtmpfsをspool先にできます。

```bash
export UNIFIED_SPOOL_STORAGE=ram
export UNIFIED_MAX_SPOOL_FILES=7200
export UNIFIED_MAX_SPOOL_BYTES=16777216
python3 rtk_nmea_unified.py
```

`UNIFIED_SPOOL_STORAGE=ram` では、`UNIFIED_SPOOL_DIR` 未指定時に `/dev/shm/qlm29h-nmea-unified/spool` を使います。
systemdで運用する場合は、`RuntimeDirectory` を使って `/run/qlm29h-nmea-unified/spool` を指定する構成も使えます。

```ini
RuntimeDirectory=qlm29h-nmea-unified
Environment=UNIFIED_SPOOL_STORAGE=ram
Environment=UNIFIED_SPOOL_DIR=/run/qlm29h-nmea-unified/spool
Environment=UNIFIED_MAX_SPOOL_FILES=7200
Environment=UNIFIED_MAX_SPOOL_BYTES=16777216
```

`UNIFIED_MAX_SPOOL_FILES` はリングバッファの最大件数です。上限に達すると古いpayloadから削除します。
`UNIFIED_MAX_SPOOL_BYTES` はspool内JSON payloadの合計byte数上限です。`0` を指定するとbyte上限を無効化します。
RAM上のspoolは再起動で消えるため、SDカード保護を優先する用途向けです。

## ライセンス

このリポジトリのコードは [MIT License](LICENSE) で提供します。
本スクリプトが利用する `pyserial` / `requests` などのライブラリは、それぞれのライセンスに従います。

## 免責事項

- 本リポジトリ配下のスクリプトは開発例（サンプル）であり、動作を保証するものではありません。
- 本スクリプトに記載されている内容は商用利用を想定していません。ご利用にあたっては、お客様の責任の範囲においてご利用ください。
