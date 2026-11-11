# Logo assets

`sadoc-logo.png` is the transparent master logo. `sadoc-readme.png` is a smaller
parchment-backed copy that stays visible in both light and dark README themes.
`sadoc/logo.png` is a smaller copy shipped inside the Python package for email.
Email delivery attaches it inline by Content-ID; no public image host is needed.

To rebuild the email image from the repository root with ImageMagick:

```sh
magick assets/sadoc-logo.png -resize 192x192 -strip sadoc/logo.png
magick assets/sadoc-logo.png -resize 480x480 -background '#faf5e8' -alpha remove -alpha off -strip assets/sadoc-readme.png
```
