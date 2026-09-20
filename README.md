# 🎵 OxygenMusic

A high-performance, lightweight native Linux music player designed for Arch Linux and custom tiling window managers (like Hyprland and Caelestia Shell), featuring an iOS 27 "Liquid Glass" aesthetic and real-time spatial visualizers.

---

## ✨ Core Features

- **iOS 27 Liquid Glass UI:** Frosted glass aesthetics, dynamic blur boundaries, and a sleek modern layout.
- **120Hz Spatial Visualizer:** Non-blocking multithreaded fast Fourier transform (FFT) powered by SciPy and NumPy for real-time reactive audio spectrums.
- **Audio Equalizer & Preamp:** Adjustable 5-band frequency filters and master preamp gain controls.
- **Auto-Play & Queue Management:** Continuous track progression for both local audio libraries and downloaded streaming searches.
- **MPRIS v2 D-Bus Integration:** Full desktop shell integration allowing global media keys (`playerctl`) to control playback seamlessly.

---

## 📦 Installation on Arch Linux

You can easily build and install OxygenMusic directly from source using `makepkg`:

```bash
# 1. Clone the repository
git clone [https://github.com/GODDark10251/OxygenMusic.git](https://github.com/GODDark10251/OxygenMusic.git)
cd OxygenMusic

# 2. Build and install the Arch package
makepkg -si
