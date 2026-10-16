# Newsletter to Email

Turn a PDF newsletter into an editable email while keeping its clickable web and email links. The PDF appears on the left; the editable email appears on the right.

## Open the app

Double-click **Newsletter Studio.exe** in the Code folder. It opens your browser and a small control window, with no terminal. Keep that control window open while editing; choose **Quit** when finished. The executable includes Python and the PDF tools, so no installation or internet connection is needed. Starting it again opens the existing app when it is already running.

The source-code launcher **Start Newsletter App.cmd** is also available for development. This source version needs Python 3.10 or newer. The included `.vendor` folder supplies the PDF reader when present. If it is missing, the launcher installs the dependency into a local `.venv` folder on the first run; that setup needs an internet connection. Subsequent conversions work offline.

## Convert and send a newsletter

1. Upload or drop a PDF, or load the October example. Files may be up to 30 MB.
2. Compare the PDF pages on the left with the email on the right. Match PDF layout preserves the complete design and clickable link regions. Choose Editable text when you want to rewrite the extracted body text; complete branded headers remain as pictures.
3. Click in the email to edit. Use Add picture, paste an image from the clipboard, or drop image files into the editor. Select a picture to move it earlier/later, align it, resize it, or remove it. Drag to reposition. A matching-layout page moves as a whole so its tiles stay aligned. Undo and Redo restore image changes.
4. Copy the formatted email, then paste it into the body of a new message in Gmail, Outlook, or another email service. Choose formatted/rich text paste if your email service offers it. Add your recipients and subject there.
5. Review the pasted message and send a test to yourself before sending to the full mailing list.

**Pictures and clipboard support vary by browser and email service.** Copying retains editable text and hyperlinks in compatible rich text editors. Some email services omit images pasted from a local page. Choose **Export → Outlook email draft (.eml)** when you want a file with the pictures embedded, especially for a desktop email application such as Outlook. The file is an unsent message with its subject, HTML, plain text alternative, and inline picture attachments. How it opens as an editable draft depends on the email application. The app does not send mail or add recipients.

Choose **Export → Email HTML (.html)** to keep an editable email version you can reopen in a browser or reuse later. Images in the downloaded HTML are embedded in the file. Keep a local copy before closing or refreshing the app if you want to preserve edits.

## What to expect

- PDF text, many text styles, hyperlinks, and optional compressed pictures are converted to email-friendly HTML.
- Match PDF layout uses email-safe picture tiles in tables, with links on the correct regions. Text within those page images is not directly editable; select Editable text to edit the body text. Editable mode preserves full-width branded headers as composite images.
- Links over pictures are retained when the PDF provides them. QR codes with supported links are also decoded into clickable links. Review any extracted links and the converted reading order.
- A scanned PDF may contain no extractable text. There is no OCR; use a PDF exported with selectable text for an editable result. Picture-only pages can still be shown when images are included.
- Compression usually makes a newsletter smaller, but the size depends on its text and pictures. The original PDF is not attached to the email. Avoid adding very large pictures when email size matters.
- PDF content is treated as newsletter material. Embedded scripts are not executed, and the app does not interpret document text as instructions.

## Privacy and storage

Conversion runs locally in Python on this computer. There are no AI calls, analytics, accounts, or cloud uploads. The server binds to `127.0.0.1` and rejects requests from other website origins. The latest three uploaded PDFs are kept in memory for page previews and are cleared when the app stops. The app does not save uploaded PDFs or email edits automatically. Explicit downloads are saved by your browser. Following a newsletter hyperlink opens its external website.

The October example is read from `../../October/PSIS 499 PTA Newsletter_ October.pdf`, relative to this app folder. It is not included in the application files. To use another example, set the environment variable `NEWSLETTER_SAMPLE` to the PDF's full path before starting the server.

## Start from a terminal

From this folder:

```powershell
python server.py
```

To choose another port or keep the browser from opening automatically:

```powershell
python server.py --port 8800 --no-browser
```

Then open `http://127.0.0.1:8800` in your browser. If a dependency is missing, run `python -m pip install -r requirements.txt` or use the launcher.

Run the backend regression checks with `python -m unittest test_server.py`.

## Application files

Keep `server.py`, `converter.py`, `index.html`, `app.js`, `styles.css`, `requirements.txt`, and the launcher together. The `.vendor` folder is optional when the dependencies in `requirements.txt` are installed in the Python environment you use.

## Rebuild the Windows executable

On Windows, install requirements-build.txt and run build_exe.py. The self-contained, windowed executable is written to dist/Newsletter Studio.exe. The desktop.py entry point manages the local server and control window. The October example is optional and is located relative to the executable, rather than embedded in it.


