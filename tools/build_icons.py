"""由单张母图生成双端应用图标资源。

【作用】
把一张正方形母图（默认 assets/icon_source.png）一次性转换成：
1. PC 端 PyInstaller 用的多尺寸 Windows .ico（assets/app.ico）；
2. Android 端各密度 mipmap（app/src/main/res/mipmap-*/ic_launcher*.png）；
3. Web 端 PWA / 站点图标（web/assets/icons/brand/icon-*.png）；
4. 界面 logo（web/assets/icons/brand/logo.png，圆角方形）——
   登录页主视觉与工具页顶栏共用同一个文件。

【为什么固化成脚本】
图标是多端共用的静态资源，手改尺寸既繁琐又容易在改母图后漏改某一端。
保留这个脚本后，换图标只需覆盖 assets/icon_source.png 再执行一次：

    py tools/build_icons.py

【用法】
    py tools/build_icons.py                 # 用默认母图重新生成全部图标
    py tools/build_icons.py path/to/new.png # 指定母图
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - 环境缺 Pillow 时给出明确指引
    print("✘ 需要 Pillow：py -m pip install Pillow")
    raise SystemExit(1)

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE: Path = PROJECT_ROOT / "assets" / "icon_source.png"
ICO_TARGET: Path = PROJECT_ROOT / "assets" / "app.ico"

ANDROID_RES: Path = PROJECT_ROOT / "android" / "app" / "src" / "main" / "res"
WEB_BRAND: Path = PROJECT_ROOT / "web" / "assets" / "icons" / "brand"

#: Windows .ico 需要内嵌的尺寸集合（覆盖任务栏小图标到大图标视图）
ICO_SIZES: tuple[int, ...] = (256, 128, 64, 48, 32, 24, 16)

#: Android 密度目录 -> 启动图标边长（px）。标准 launcher icon 尺寸：
#: mdpi 48 / hdpi 72 / xhdpi 96 / xxhdpi 144 / xxxhdpi 192。
ANDROID_MIPMAPS: dict[str, int] = {
    "mipmap-mdpi": 48,
    "mipmap-hdpi": 72,
    "mipmap-xhdpi": 96,
    "mipmap-xxhdpi": 144,
    "mipmap-xxxhdpi": 192,
}

#: Web PWA 图标尺寸
WEB_SIZES: tuple[int, ...] = (192, 512)

#: 界面 logo（登录页 .gate-mark / 顶栏 .brand 共用）的输出边长。
#: 【为什么是 512】登录页最大显示 52px（见 input.css），512 覆盖到 ~9.8× 屏，
#: 足以应对 4K/Retina 下的 200% 缩放。当前母图 1024×1024，512 是干净的下采样，
#: 无插值放大。若母图将来退化到 128，本值自动变成 4× 放大（会糊但不报错）。
LOGO_SIZE: int = 512

#: 界面 logo 的圆角半径占边长比。
#: 【2026-10-05 定稿 0.23】用 4× 放大对比页实测过 0.16 / 0.19 / 0.23 / 0.29：
#: 0.16 在圆角容器（22px/76px≈0.29）里显得偏方、不协调；0.19 是同心圆角
#: 理论值（22−12=10px ÷ 52px）但仍略锐；0.23 与容器弧度呼应且保留层次，
#: 目测最协调；0.29 与容器同弧会「圆角套圆角」融成一个形状，弃用。
LOGO_RADIUS_RATIO: float = 0.23


def _load_square(source: Path) -> Image.Image:
    """读取母图并保证为正方形 RGBA。"""
    if not source.is_file():
        raise FileNotFoundError(f"母图不存在：{source}")
    img = Image.open(source).convert("RGBA")
    w, h = img.size
    if w != h:
        # 非正方形时居中裁切为正方形，避免拉伸变形
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        print(f"⚠ 母图非正方，已居中裁切为 {side}×{side}")
    return img


def _resize(img: Image.Image, size: int) -> Image.Image:
    """高质量缩放（LANCZOS 对插画类图标最平滑）。"""
    return img.resize((size, size), Image.LANCZOS)


def build_ico(src: Image.Image, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # ICO 保存时以最大尺寸为基准，Pillow 会按 sizes 生成全部内嵌尺寸
    base = _resize(src, max(ICO_SIZES))
    base.save(target, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"✔ PC 图标已生成：{target.relative_to(PROJECT_ROOT)} "
          f"（内嵌 {len(ICO_SIZES)} 种尺寸）")


def build_android(src: Image.Image) -> None:
    for folder, size in ANDROID_MIPMAPS.items():
        out_dir = ANDROID_RES / folder
        out_dir.mkdir(parents=True, exist_ok=True)
        icon = _resize(src, size)
        icon.save(out_dir / "ic_launcher.png", format="PNG")
        # 圆形图标：把方形切进圆形蒙版，保证部分厂商 Launcher 显示为圆形
        circle = _make_round(icon)
        circle.save(out_dir / "ic_launcher_round.png", format="PNG")
    print(f"✔ Android 图标已生成：{len(ANDROID_MIPMAPS)} 个密度目录 "
          f"（ic_launcher.png + ic_launcher_round.png）")


def _make_round(icon: Image.Image) -> Image.Image:
    """用抗锯齿圆形蒙版裁出圆形图标。"""
    from PIL import ImageDraw

    size = icon.size[0]
    ss = 4  # 超采样倍率，抵消极小尺寸下的锯齿
    mask = Image.new("L", (size * ss, size * ss), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * ss - 1, size * ss - 1), fill=255)
    mask = mask.resize(icon.size, Image.LANCZOS)
    out = Image.new("RGBA", icon.size, (0, 0, 0, 0))
    out.paste(icon, (0, 0), mask)
    return out


def build_web(src: Image.Image) -> None:
    WEB_BRAND.mkdir(parents=True, exist_ok=True)
    for size in WEB_SIZES:
        _resize(src, size).save(WEB_BRAND / f"icon-{size}.png", format="PNG")
    # favicon 也顺手更新，保持浏览器标签页一致
    _resize(src, 32).save(WEB_BRAND / "favicon.png", format="PNG")
    print(f"✔ Web 图标已生成：{WEB_BRAND.relative_to(PROJECT_ROOT)} "
          f"（{', '.join(f'icon-{s}.png' for s in WEB_SIZES)} + favicon.png）")


def _make_rounded(icon: Image.Image, radius_ratio: float) -> Image.Image:
    """用抗锯齿圆角矩形蒙版裁出圆角方形图标（非纯圆形）。

    与 ``_make_round`` 同样走 4× 超采样：小尺寸下直接画蒙版锯齿很明显。
    """
    from PIL import ImageDraw

    size = icon.size[0]
    ss = 4
    radius = max(1, int(round(size * radius_ratio))) * ss
    mask = Image.new("L", (size * ss, size * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size * ss - 1, size * ss - 1), radius=radius, fill=255
    )
    mask = mask.resize(icon.size, Image.LANCZOS)
    out = Image.new("RGBA", icon.size, (0, 0, 0, 0))
    out.paste(icon, (0, 0), mask)
    return out


def build_web_logo(src: Image.Image) -> None:
    """生成界面 logo（登录页主视觉 + 顶栏品牌位共用同一个文件）。

    【为什么由本脚本而非 extract_game_assets 生成】
    该文件原先是游戏包 ``Logo_01.png`` 的提取产物；但需求要求它与
    EXE / APK 图标共用同一张头像，故改由本脚本从同一母图派生 ——
    换图标从此只需覆盖 ``assets/icon_source.png``。相应地，
    ``extract_game_assets.GAME_ASSET_MAP`` 里的 logo 映射必须保持移除状态。
    """
    WEB_BRAND.mkdir(parents=True, exist_ok=True)
    logo = _make_rounded(_resize(src, LOGO_SIZE), LOGO_RADIUS_RATIO)
    logo.save(WEB_BRAND / "logo.png", format="PNG")
    print(f"✔ 界面 logo 已生成：{WEB_BRAND.relative_to(PROJECT_ROOT)}/logo.png "
          f"（{LOGO_SIZE}×{LOGO_SIZE} 圆角方形，半径比 {LOGO_RADIUS_RATIO}）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", nargs="?", default=str(DEFAULT_SOURCE),
                        help="母图路径（正方形 PNG，默认 assets/icon_source.png）")
    args = parser.parse_args(argv)

    source = Path(args.source).resolve()
    src = _load_square(source)
    print(f"母图：{source}（{src.size[0]}×{src.size[1]} RGBA）\n")

    build_ico(src, ICO_TARGET)
    build_android(src)
    build_web(src)
    build_web_logo(src)

    print("\n全部图标生成完毕。重新封包即可生效：")
    print("  PC     : py tools/release.py pc")
    print("  Android: py tools/release.py android")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
