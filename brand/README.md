# Smarter Zones — brand assets

A white house split into three independently sensed zones (cool / comfortable /
warm dots) on a sky-to-deep-blue tile.

## Files
- `icon.svg`, `logo.svg` — scalable, editable sources.
- `icon.png` (256), `icon@2x.png` (512) — square app icon.
- `logo.png` (256 tall), `logo@2x.png` (512 tall) — wordmark for light UIs.
- `dark_logo.png`, `dark_logo@2x.png` — wordmark for dark UIs (light text).
- `custom_integrations/smarterzones/` — the same PNGs laid out ready for the
  Home Assistant brands repository.

## Showing it in Home Assistant
Home Assistant pulls integration icons/logos from the **home-assistant/brands**
repository by domain — they aren't read from the integration folder itself. To
get the Smarter Zones logo to appear in the UI:

1. Fork `https://github.com/home-assistant/brands`.
2. Copy the contents of `custom_integrations/smarterzones/` into
   `custom_integrations/smarterzones/` in your fork (domain must match the
   integration's `domain`, i.e. `smarterzones`).
3. Open a pull request. Once merged, the icon/logo show up automatically.

Brands requirements met here: transparent background, square icon at 256/512,
and hDPI `@2x` variants. The icon works on both light and dark themes, so a
separate `dark_icon` isn't needed; dark logo variants are included for the
wordmark.

For your repo's README/HACS, you can reference `logo.png` directly.
