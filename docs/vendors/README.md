# Vendor documentation

Manufacturer specs, kept in the repo so the reasoning in `docs/design.md` can be
checked against a primary source rather than recollection.

## Yamaha

`yamaha/DM7_osc_specs_V110_en.pdf` — *DM7 Series OSC Specifications*, V1.1.0,
July 2025. The authority for everything in design.md §5.3.

`yamaha/DM7_osc_specs_V110_en.txt` — a text extraction of the same document, for
grep. Derived and disposable; the PDF wins.

### These PDFs are encrypted

Yamaha ships them **AES-128 encrypted with an empty user password** — owner
restrictions only, so they open fine in any viewer while every internal stream
is encrypted.

The practical consequences, and the reason this note exists:

- Claude Code's file reader fails on them (it shells out to `pdftoppm`).
- Naive `zlib` inflation of the page streams also fails. **That failure is the
  encryption, not corruption.** The symptom — 2 of 92 streams inflating — looks
  exactly like a broken parser, which is a good way to lose an hour.
- Text extraction needs the standard security handler: derive the file key by
  MD5 over the 32-byte pad + `/O` + `/P` little-endian signed + `ID[0]`, then 50
  MD5 iterations; the per-object key is
  `MD5(key + objnum[:3] + gen[:2] + "sAlT")`. AES-128-CBC, first 16 bytes are
  the IV. Then inflate.
- Decode text with **per-font** `ToUnicode` CMaps. Merging them globally across
  subset fonts yields plausible-looking but wrong glyphs — `HLVtory` for
  `History` — which is worse than an obvious failure.

Two things that cost time afterwards: `grep` treats the extracted text as binary
and silently reports nothing, so use `grep -a`; and cluster text rows with a
tolerance smaller than the line pitch, or adjacent table rows merge and scramble
their columns.

The DM7 **MIDI data format** document is an open item (design.md §7) and will
need the same treatment.
