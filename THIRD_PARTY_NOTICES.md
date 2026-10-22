# Third-party acknowledgements

Asteria uses the following open-source projects and services. Each dependency and bundled resource remains subject to its own license; the project license does not replace third-party terms.

| Project | Role | License / notice |
| --- | --- | --- |
| [EasyRead](https://github.com/Edwardxlai/easyread) | Architectural and interaction-design reference for structured bilingual reading, page translation, and annotations | MIT; design reference, not bundled implementation |
| [PDF.js](https://github.com/mozilla/pdf.js) | Bundled PDF viewer, worker, CMaps, fonts, and WASM resources | Apache-2.0 and embedded asset notices in `paper_reader/web/vendor/pdfjs/` |
| [pdfplumber](https://github.com/jsvine/pdfplumber) | PDF text and layout extraction | MIT |
| [pdfminer.six](https://github.com/pdfminer/pdfminer.six) | PDF parsing | MIT |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | PDF rasterization used by dependencies | Apache-2.0 / BSD-3-Clause; PDFium notices retained in binary distributions |
| [pywebview](https://github.com/r0x0r/pywebview) | Native desktop window | BSD-3-Clause |
| [pystray](https://github.com/moses-palmer/pystray) | System tray | LGPL-3.0; shipped as replaceable external Python source in binary distributions |
| [Pillow](https://github.com/python-pillow/Pillow) | Image handling | MIT-CMU |
| [FastAPI](https://github.com/fastapi/fastapi), [Starlette](https://github.com/Kludex/starlette), [Uvicorn](https://github.com/encode/uvicorn) | Local application service | MIT / BSD-3-Clause |
| [Python](https://www.python.org/) and Python packages | Runtime and support libraries | Original license files are included in binary distributions |
| [Docling](https://github.com/docling-project/docling) | Optional external document parser and OCR | MIT for project code; separately installed models retain their own licenses |

The complete notices for bundled Python packages are included under `third-party-notices/` in Windows distributions, with package versions in `versions.json`. PDF.js licenses and its embedded font, CMap, image-codec, and color-management notices are retained next to the corresponding resources. No third-party service endorses Asteria.
