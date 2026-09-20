# Maintainer: Chetham Gladwin <chetham@oxygenmusic.local>
pkgname=oxygen-music
pkgver=1.0.0
pkgrel=1
pkgdesc="A high-performance Linux music player featuring iOS 27 Liquid Glass aesthetics and spatial visualizers"
arch=("any")
url="https://github.com/chethamgkadwin/oxygenmusic"
license=("MIT")
depends=("python" "python-pyqt6" "python-scipy" "python-numpy" "ffmpeg" "yt-dlp" "python-requests")
source=("oxygen_music.py" "oxygenmusic.desktop" "oxygenmusic.svg")
sha256sums=("SKIP" "SKIP" "SKIP")

package() {
    install -Dm755 "$srcdir/oxygen_music.py" "$pkgdir/usr/share/oxygenmusic/oxygen_music.py"
    
    install -dm755 "$pkgdir/usr/bin"
    ln -s "/usr/share/oxygenmusic/oxygen_music.py" "$pkgdir/usr/bin/oxygenmusic"

    install -Dm644 "$srcdir/oxygenmusic.desktop" "$pkgdir/usr/share/applications/oxygenmusic.desktop"
    install -Dm644 "$srcdir/oxygenmusic.svg" "$pkgdir/usr/share/icons/hicolor/scalable/apps/oxygenmusic.svg"
}
