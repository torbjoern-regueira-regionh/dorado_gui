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

## Resume a crashed run
If basecalling crashed or the PC restarted, the reads that were already written do not have to be basecalled again.

1. Keep **pod5 folder**, model, modified bases, barcode kit and the other options the same as in the crashed run. Dorado refuses to resume with a different model.
2. Tick **Resume from BAM folder** and select the crashed run's output folder. Every `.bam` below it is used, so choosing the folder that contains `bam_pass` also includes `bam_fail`.
3. Choose a **new, empty output folder**. The GUI refuses an output folder inside or around the old one, so the old files can never be overwritten.
4. **Start**. The GUI first joins the old BAM files into one temporary file (`_resume_input.bam` in the new output folder), because `dorado --resume-from` accepts only a single file. Then it starts dorado, which copies those reads into the new output and basecalls only the remaining ones.

- Reads are copied unchanged, so modified-base calls (MM/ML tags), barcodes and all other tags are kept.
- BAM files that were cut off by the crash are fine: their complete reads are used and the broken end is dropped. This is listed in the log.
- The old BAM files are only read. Once the resumed run has finished and you have checked it, the old output folder can be deleted; the new one is complete.
- You need free disk space of about twice the size of the old BAM files. The temporary file is removed when dorado exits.
- If the resumed run crashes too, resume again from its output folder (with another new output folder), unless it crashed in the first minutes while still copying the old reads; then resume from the original folder again.
- No extra software is needed; joining uses only Python.

Settings are remembered in `%APPDATA%\dorado_gui\settings.json`.
