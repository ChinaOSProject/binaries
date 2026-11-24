# ChinaOSProject APT 仓库

APT 发布 `stable/main`。索引按 `.deb` 控制字段中的 `Architecture` 分类，`Architecture: all` 的包进入各架构索引。LoongArch 路径、包文件名后缀和包内 `Architecture` 统一使用本机 `dpkg --print-architecture` 返回的 `loongarch64`。索引器拒绝包内声明为 `loong64` 的新包，保留实际 `amd64` 包的独立索引。

## 添加软件源

将 `base` 设置为已部署的 Worker HTTPS 地址。先从独立可信渠道获取发布者公布的 OpenPGP 指纹，再核对下载的公钥。

```sh
base=https://chinaos-apt.YOUR-SUBDOMAIN.workers.dev
curl -fsSL "$base/chinaos-archive-keyring.gpg" -o chinaos-archive-keyring.gpg
gpg --show-keys --with-fingerprint chinaos-archive-keyring.gpg
```

确认指纹后安装公钥和软件源。

```sh
curl -fsSL "$base/chinaos.sources" -o chinaos.sources
sudo install -m 0644 chinaos-archive-keyring.gpg /usr/share/keyrings/
sudo install -m 0644 chinaos.sources /etc/apt/sources.list.d/
sudo apt-get update
```

`.sources` 通过 `Signed-By` 限定仓库使用的公钥。仓库设为 `NotAutomatic` 和 `ButAutomaticUpgrades`，可以显式安装其软件包，并接收已安装包的更新。

## GCC 13 运行库更名

`chinaos-libgcc-s1` 是本仓库的私有 GCC 13 运行库，原名 `clash-libgcc-s1`。已安装旧包的设备在更新软件源后，可运行 `sudo apt install chinaos-libgcc-s1` 切换。新包继续满足已发布软件对旧包名的版本依赖，并与系统发行版的 `libgcc1` 并存。

## 首次部署

1. 创建私有 R2 桶 `chinaos-apt-index`，创建一个仅能读写该桶对象的 R2 S3 API 凭据。若要使用其它桶名，同时修改 [Worker 配置](apt/wrangler.jsonc) 和下面的 `APT_R2_BUCKET`。
2. 准备专用的 OpenPGP 归档签名密钥，备份密钥和吊销证书，并公布完整指纹。CI 需要一份能无交互签名的密钥或签名子密钥。将可导入的私钥导出内容做单行 Base64 编码后保存到 GitHub Actions Secret。
3. 在 GitHub 仓库配置以下 Actions 变量和 Secret。

   | 类型 | 名称 | 值 |
   | --- | --- | --- |
   | Variable | `CLOUDFLARE_ACCOUNT_ID` | Cloudflare 账户 ID |
   | Variable | `APT_SIGNING_FINGERPRINT` | 签名密钥的完整 40 位 OpenPGP 指纹，或所属主密钥的完整指纹 |
   | Variable | `APT_R2_BUCKET` | 可选，默认 `chinaos-apt-index` |
   | Secret | `APT_SIGNING_KEY_B64` | Base64 编码的 CI 签名私钥 |
   | Secret | `APT_R2_ACCESS_KEY_ID` | 该桶的 S3 Access Key ID |
   | Secret | `APT_R2_SECRET_ACCESS_KEY` | 对应的 S3 Secret Access Key |

4. 在 Cloudflare 账户内授权 Wrangler 后部署 Worker。在 GitHub Actions 运行一次 `Publish APT index`，随后用上面的步骤验证客户端。

   ```sh
   npx wrangler deploy --config apt/wrangler.jsonc
   ```

## 索引更新和包下载

- [GitHub Actions](.github/workflows/apt-index.yml) 在 `main` 更新时运行，也支持手动运行。每周重签一次，`Release` 有效期为 14 天。
- [索引生成器](apt/build-index.py) 读取指定提交中的 LFS 指针和真实 Debian 控制字段。它复用已验证的控制信息，对新对象先用 HTTP Range 读取控制归档并核对 GitHub LFS 的 ETag、对象长度。需要完整下载时核对 `.deb` 的 SHA-256。同名、同版本、同架构的不同内容会使发布失败。
- 工作流把签名的 `InRelease`、`Release`、`Release.gpg`、`Packages`、`Packages.gz` 和公钥写入 R2，全部上传成功后再切换 `current`。保留旧 `by-hash/` 对象，让正在更新的客户端仍能取得原索引。
- [Worker](apt/worker.mjs) 提供标准 `dists/stable` 和 `pool` 路径。每个包 URL 包含生成索引时的 Git 提交，Worker 返回 HTTP 307 到该提交对应的 GitHub LFS 媒体地址。APT 按签名索引中的 SHA-256 校验下载，R2 只存索引和公钥。下载使用 GitHub LFS 流量配额。
