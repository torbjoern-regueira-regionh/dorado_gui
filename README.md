# Dorado Basecaller GUI

A simple Windows front-end for `dorado basecaller`. It only uses the Python standard library (tkinter).

## Install (once per PC)
1. Make sure Python 3.10+ is installed along with git.
2. Unzip dorado (for example `C:\dorado-2.1.0-win64`).
3. Clone this repo, then optionally copy `dorado_gui.pyw` and `Start Dorado GUI.bat` into the same folder.

## Use
Double-click `dorado_gui.pyw`.

1. **dorado executable**: `...\dorado-x.y.z-win64\bin\dorado.exe`. The GUI finds it automatically if it is on PATH or in `C:\`, Program Files, home or Downloads.
2. **Models directory** (recommended): a fixed folder such as `D:\dorado_models`. Models download once and are reused, and runs work offline after that.
3. **pod5 folder** and **output folder**.
4. **Model**:
   - *Standard*: choose DNA/RNA, fast/hac/sup, a version ("latest" or a specific one) and modified bases. Dorado picks the right model for the pod5 chemistry. The GUI allows only one modification model per base (one C model, one A model, and so on).
   - *Local model folder*: point to a model you downloaded yourself, plus modbase model folders.
   - **Refresh model list from dorado** runs `dorado download --list`, so newly released models and modifications show up without changing the code.
5. Options: barcode kit, `--no-trim`, min Q-score, FASTQ output, alignment reference, poly(A) estimation, device and free-text extra arguments.
6. **Start**. The output appears in the log window and is also saved as `dorado_gui_<timestamp>.log` in the output folder. **Copy** copies the exact command line.

Settings are remembered in `%APPDATA%\dorado_gui\settings.json`.
