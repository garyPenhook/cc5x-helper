# `cc5x_setcc_native` Support Matrix

This matrix reflects the current state of the pack-first native replacement work in this repository.

Scope target for CC5X-family devices:

- `PIC10F`
- `PIC12F`
- `PIC16F`

It does not claim support for PIC18, because CC5X is not the target there.

## Levels

- `Compiler-validated`
  - generated header compiled successfully with the real `CC5X.EXE` under CrossOver
  - shipped-header control build also compiled successfully
- `Pack-discoverable`
  - local `.atpack` metadata is present and the device resolves through `probe`
  - not yet compile-validated in this repo
- `Shipped-header-only`
  - a CC5X header exists locally, but no local pack metadata has been found

## Compiler-Validated Devices

Validated using [validate_generated_headers.py](/home/gary/apps/cc5x_paid/tools/validate_generated_headers.py) and the installed CrossOver bottle:

| Device | Family | Generated Header | Shipped Header Control | Result |
|---|---|---:|---:|---|
| `PIC10F200` | `10F` | yes | yes | pass |
| `PIC10F320` | `10F` | yes | yes | pass |
| `PIC10F322` | `10F` | yes | yes | pass |
| `PIC12F1501` | `12F` | yes | yes | pass |
| `PIC12F1840` | `12F` | yes | yes | pass |
| `PIC16F1509` | `16F` | yes | yes | pass |
| `PIC16F15313` | `16F` | yes | yes | pass |
| `PIC16F1789` | `16F` | yes | yes | pass |
| `PIC16F18325` | `16F` | yes | yes | pass |
| `PIC16F18446` | `16F` | yes | yes | pass |
| `PIC16F18857` | `16F` | yes | yes | pass |
| `PIC16F19195` | `16F` | yes | yes | pass |

For the minimal validation source, each successful build produced:

- `.hex`
- `.occ`
- `Total of 3 code words (0 %)`

## Pack-Discoverable Scope On This Machine

The installed local `.atpack` files currently expose a broad `10F`/`12F`/`16F` population:

- approximately `303` local `10F`/`12F`/`16F` device metadata entries were discovered from the installed packs
- examples include:
  - `PIC10F200`
  - `PIC10F320`
  - `PIC12F1501`
  - `PIC12F1840`
  - `PIC16F1509`
  - `PIC16F15313`
  - `PIC16F1789`
  - `PIC16F18446`
  - `PIC16F18857`
  - `PIC16F19195`

These are candidates for future compile validation, and they can now be listed directly with:

```bash
python3 tools/cc5x_setcc_native.py list-devices
```

## Shipped Header Coverage On This Machine

The bundled BKND header set contains approximately `239` `10F`/`12F`/`16F` headers.

This is useful for:

- compatibility comparison
- alias-policy refinement
- control builds during validation

But shipped headers are not treated as authoritative coverage for newer devices.

## `10F` Status

Current status:

- `PIC10F` is in-scope for CC5X family support
- local `10F` pack metadata is now present from `Microchip.PIC10-12Fxxx_DFP.1.8.184.atpack`
- compiler validation has been completed for:
  - `PIC10F200`
  - `PIC10F320`
  - `PIC10F322`

This means the validated family coverage now spans:

- `10F`
- `12F`
- `16F`

## Current Boundaries

The current implementation is strong enough to claim:

- pack-native config workflows for locally available `10F`/`12F`/`16F` devices
- real-compiler-validated generated headers for the twelve devices listed above
- CrossOver-backed validation workflow repeatability
- a first-pass open project workflow via `setcc-native.json`, including manifest init, validation, config sync, and manifest-driven builds

It is not yet complete enough to claim:

- full validated coverage of all local `10F`/`12F`/`16F` packs
- finished parity with every BKND hand-tuned header
- full parity with every `setcc.pxk` GUI preference or edition behavior
