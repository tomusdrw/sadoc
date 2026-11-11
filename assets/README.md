# Logo assets

`sadoc-logo.png` is the transparent master logo used in the project README.
`sadoc/logo.png` is a smaller copy shipped inside the Python package for email.
Email delivery attaches it inline by Content-ID; no public image host is needed.

To rebuild the email image from the repository root with ImageMagick:

```sh
magick assets/sadoc-logo.png -resize 192x192 -strip sadoc/logo.png
```
