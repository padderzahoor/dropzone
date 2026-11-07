# Dropzone

**Simple local Wi-Fi file sharing through the browser.**  
No installation, no accounts, no cloud. Just run one Python file and open the link on any device on the same network.

Perfect for quickly moving large files (GBs) between your phone, laptop, and other devices without cables or slow cloud uploads.

## Features

- Works on any device with a modern browser (iPhone, Android, Windows, macOS, Linux)
- Drag & drop or file picker
- Supports very large files (streams directly to disk — low memory usage)
- Real-time transfer speed display
- Optional PIN protection
- Clean, modern UI with dark mode support
- Zero dependencies (pure Python 3.8+)
- Resume-friendly downloads (HTTP Range support)

## Quick Start

```bash
python server.py
```

Then open the printed address (e.g. `http://192.168.1.42:8000`) on any device connected to the same Wi-Fi.

### Options

```bash
python server.py --port 8080          # change port
python server.py --dir myfiles        # change storage folder
python server.py --pin 1234           # require a PIN
python server.py --pin                # generate a random 4-digit PIN
```

Files are saved in the `shared` folder (or the folder you specify with `--dir`).

## Optional: QR Code

If you have the `qrcode` package installed, a QR code will be printed so you can scan it from your phone:

```bash
pip install qrcode
```

## How it works

1. You run `server.py` on one computer (the host).
2. The script starts a lightweight web server on your local network.
3. Any device on the same Wi-Fi can open the link and upload/download files.
4. Files are stored on the host computer in the chosen folder.

Everything stays on your local network — nothing is sent to the internet.

## Requirements

- Python 3.8 or newer
- Devices must be on the **same Wi-Fi network**

That's it. No extra packages needed.

## License

MIT
