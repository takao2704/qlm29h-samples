# QLM29H Samples

Quectel QLM29HBAA-GM 用の RTK（NTRIP）サンプルスクリプト集です。

## ファイル構成

| ファイル | 説明 |
|---|---|
| [rtk_client.py](rtk_client.py) | NTRIPクライアント。RTCM補正をモジュールに転送し、Fix状態をログ表示する基本サンプル |
| [rtk_harvest.py](rtk_harvest.py) | `rtk_client.py` に加え、GNSS位置情報を SORACOM Harvest Data（Unified Endpoint）へ定期送信するサンプル |

## ドキュメント

| ファイル | 説明 |
|---|---|
| [docs/sequence.md](docs/sequence.md) | `rtk_client.py` / `rtk_harvest.py` の実行シーケンス |

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

## ライセンス

このリポジトリのコードは [MIT License](LICENSE) で提供します。
本スクリプトが利用する `pyserial` / `requests` などのライブラリは、それぞれのライセンスに従います。

## 免責事項

- 本リポジトリ配下のスクリプトは開発例（サンプル）であり、動作を保証するものではありません。
- 本スクリプトに記載されている内容は商用利用を想定していません。ご利用にあたっては、お客様の責任の範囲においてご利用ください。
