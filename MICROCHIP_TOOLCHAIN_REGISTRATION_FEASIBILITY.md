# CC5X Microchip Toolchain Registration Feasibility

## Result

Direct first-class registration of CC5X as `microchip.toolchains:cc5x@3.8C` is not currently viable through the normal Microchip VS Code user-facing path.

The installed `microchip.toolchains` extension has a real descriptor/build schema, but its custom-path registration path scans only a hardcoded list of supported compiler application IDs:

- `xc32`
- `xc16`
- `xc8`
- `xc-dsc`
- `arm-gcc`
- `avr-gcc`
- `pic-as`
- `avr-asm2`
- `xc-llvm`

Because `cc5x` is not in that list, `MPLAB: Add Toolchain` / `mplab.toolchains.customPaths` will not discover a CC5X installation by descriptor alone.

## Evidence Checked

Local extension:

- `~/.vscode/extensions/microchip.toolchains-1.7.3`

Files inspected:

- `package.json`
- `readme.md`
- `dist/resources/schema/opt/descriptor.schema.json`
- `dist/resources/schema/opt/builds.schema.json`
- `resources/opt/xc8/descriptor-mplab-opt.json`
- `resources/opt/xc8/builds-mplab-opt.json`
- `resources/opt/pic-as/descriptor-mplab-opt.json`
- `dist/extension.js`

Local Microchip application finder cache:

- `~/.mplab/app-finder/shelf/shelf.json`
- `~/.mplab/app-finder/apps`

Installed toolchain descriptor example:

- `/opt/microchip/xc32/v5.00/mplab_xc32/descriptor-mplab-opt.json`
- `/opt/microchip/xc32/v5.00/mplab_xc32/builds-mplab-opt.json`

## What Microchip's Descriptor System Supports

The descriptor schema supports compiler-install JSON files named:

- `descriptor-mplab-opt.json`
- `*-options-mplab-opt.json`
- `builds-mplab-opt.json`

The top-level descriptor can define:

- `version`
- `binaries`
  - `gcc`
  - `g++`
  - `as`
  - `linker`
  - `archiver`
  - `bin2hex`
  - `objcopy`
  - `objdump`
- `options.descriptors`
- `builds.descriptors`
- `builds.extensions`
- computed `properties`

The build descriptor can define:

- build types such as `standard` and `archive`
- build steps
- input extension matching
- command-line emitters
- activation expressions
- option emitters tied to option descriptor nodes

This is enough to describe a wrapper-driven CC5X command line in principle.

## Why Normal Registration Fails For CC5X

The `microchip.toolchains` custom path flow does not scan an arbitrary folder for any descriptor and register whatever `id` it finds.

The observed flow is:

1. User runs `MPLAB: Add Toolchain`, or `mplab.toolchains.customPaths` changes.
2. Extension calls `registerCustomToolchain(path)`.
3. That loops over the hardcoded `SUPPORTED_COMPILERS` list.
4. For each known compiler ID, it asks Microchip's application finder to `scanDirectories(id, path)`.
5. Only matching known app IDs are registered.

There is a separate descriptor reader that can scan a folder containing `descriptor-mplab-opt.json`, but the normal custom-path registration flow does not use it for unknown compiler IDs.

## Fake-XC8 Option

A fake-XC8 compatibility trick is possible but not recommended.

In theory, CC5X could be wrapped to look like an `xc8` installation:

- provide an `xc8-cc` wrapper
- print an XC8-looking version string
- satisfy Microchip app-finder rules for `xc8`
- ship `descriptor-mplab-opt.json` under that fake install
- translate emitted XC8-like build flags into CC5X flags

Reject this for normal implementation:

- VS Code and MPLAB UI would identify the compiler as XC8, not CC5X.
- Device support and options would be semantically wrong.
- Debug/build metadata would be misleading.
- It could corrupt user expectations and future maintenance.

Use it only as a throwaway lab experiment if absolutely necessary to understand the build pipeline.

## Patch-Microchip-Extension Option

Patching the installed Microchip extension could work technically:

- add `cc5x` to `SUPPORTED_COMPILERS`
- provide app-finder metadata or bypass app-finder
- add descriptor resources under `resources/opt/cc5x`
- register providers through the same internal service path

Reject this for production:

- it edits vendor extension files under `~/.vscode/extensions`
- updates will overwrite it
- the bundled JavaScript is minified
- it depends on private APIs and internal service names
- it would be hard to support across Microchip extension versions

This remains useful only as a controlled proof-of-concept branch.

## Companion Extension Option

The best implementation remains a companion extension.

The companion extension should:

- implement its own `CC5X` task provider
- call `tools/cc5x_setcc_native.py`
- publish diagnostics directly
- generate Microchip programming tasks using `MPLAB-DebugAdapter`
- optionally generate `.mplab.json` sidecar files for project/device context
- avoid claiming provider ID `microchip.toolchains:cc5x@3.8C` unless Microchip exposes or accepts a supported registration path

This is the stable route.

## Possible Deep Integration Route

If deeper integration is still desired, the next experiment should be a separate VS Code extension that depends on Microchip's core services and tries to access the same provider/toolchain services used by `microchip.toolchains`.

Research target:

- locate whether Microchip exposes `ToolchainService` and `ProviderService` through extension exports or only through private service wiring
- determine whether a third-party extension can call those services without importing bundled private code
- register a `cc5x` provider from our own extension process

Abort if:

- no public export is available
- service access requires importing minified private modules from the Microchip extension directory
- registration does not persist cleanly across reload
- project build still assumes Microchip compiler family behavior

Follow-up research for patching and private-provider experiments is captured in:

- `MICROCHIP_PATCHING_PRIVATE_INTEGRATION_RESEARCH.md`

That note narrows the viable patch to a direct `registerCustomToolchain` descriptor-bypass patch. It also documents why a normal third-party private-service integration is not clean with the currently installed Microchip exports.

## Descriptor Sketch

An experimental descriptor sketch is included under:

- `experimental/microchip-toolchain/cc5x/descriptor-mplab-opt.json`
- `experimental/microchip-toolchain/cc5x/builds-mplab-opt.json`

It is schema-shaped and useful for future experiments, but it is not enough to make `MPLAB: Add Toolchain` discover CC5X because the scanner does not include `cc5x` in its supported app IDs.

## Recommendation

Do not spend implementation time on first-class Microchip toolchain registration right now.

Build the CC5X companion extension first. Keep the descriptor sketch as research material, then revisit internal registration only after the companion extension has working build, diagnostics, artifacts, and Microchip program-task generation.
