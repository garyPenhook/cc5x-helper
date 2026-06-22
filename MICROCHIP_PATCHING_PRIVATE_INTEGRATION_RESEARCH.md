# CC5X Microchip Patching and Private-Service Integration Research

## Result

Patching Microchip's installed VS Code extensions is technically possible, but the useful patch is deeper than adding `cc5x` to a list. A private-service integration from a separate extension is also possible to prototype, but there is no clean public API surface in the currently installed Microchip extensions.

Use these paths only as lab phases after the standalone CC5X companion extension works.

## Probe Evidence

A repeatable local probe is included at:

- `experimental/microchip-toolchain/probe-microchip-private-integration.js`

Current result on this machine:

- `microchip.toolchains` patch anchors are present.
- Two exact `SUPPORTED_COMPILERS` arrays were found.
- `registerCustomToolchain`, `InstalledApplication`, and `getToolchainDescriptor` anchors were found.
- `microchip.toolchains` exports only `activate` and `deactivate`.
- `microchip.mplab-extensions-core` exports only `CorePlugin`, `activate`, `coreInsightsInstance`, and `deactivate`.
- Neither installed package manifest contributes a public `contributes.mplab` block.
- The core bundle still contains private provider lookup/scan code.

The CC5X experimental descriptor files validate against Microchip's local schemas:

- `experimental/microchip-toolchain/cc5x/descriptor-mplab-opt.json`
- `experimental/microchip-toolchain/cc5x/builds-mplab-opt.json`

## Local Versions Investigated

- `microchip.toolchains` 1.7.3
- `microchip.runcmake` 1.7.1
- `microchip.mplab-extensions-core` 2.0.5
- Microchip app-finder shelf under `~/.mplab/app-finder/shelf/shelf.json`

Primary files inspected:

- `~/.vscode/extensions/microchip.toolchains-1.7.3/package.json`
- `~/.vscode/extensions/microchip.toolchains-1.7.3/dist/extension.js`
- `~/.vscode/extensions/microchip.toolchains-1.7.3/dist/resources/schema/opt/descriptor.schema.json`
- `~/.vscode/extensions/microchip.toolchains-1.7.3/dist/resources/schema/opt/builds.schema.json`
- `~/.vscode/extensions/microchip.mplab-extensions-core-2.0.5/dist/extension.js`
- `~/.vscode/extensions/microchip.mplab-extensions-core-2.0.5/package.json`

## Normal Registration Blocker

The `microchip.toolchains` extension does not scan an arbitrary folder for an arbitrary `descriptor-mplab-opt.json` and register whatever compiler ID it finds.

Its custom-path registration flow is:

1. Read paths from `mplab.toolchains.customPaths` or the `MPLAB: Add Toolchain` command.
2. Loop over a hardcoded `SUPPORTED_COMPILERS` list.
3. For each known compiler ID, ask the Microchip app-finder to scan the selected folder.
4. Register only returned `InstalledApplication` instances.

The observed list is:

```text
xc32
xc16
xc8
xc-dsc
arm-gcc
avr-gcc
pic-as
avr-asm2
xc-llvm
```

`cc5x` is not in that list, and the app-finder shelf does not define CC5X as a known application. That means a CC5X folder with valid Microchip-style descriptor JSON still will not be found through the normal UI.

## Patching Route

### Minimal List Patch

Patch:

```js
SUPPORTED_COMPILERS=[..., "xc-llvm", "cc5x"]
```

Expected result:

- The scanner will attempt `scanDirectories("cc5x", selectedPath)`.

Expected failure:

- App-finder still does not know what `cc5x` is.
- It likely returns no installed application.
- No `InstalledApplication` means `registerToolchains()` is never called.

Conclusion: this patch is insufficient by itself.

### App-Finder Shelf Patch

Patch:

- Add a CC5X application definition to the app-finder shelf/cache.
- Add `cc5x` to `SUPPORTED_COMPILERS`.
- Provide matching install layout and version discovery.

Expected result:

- Custom-path scanning might return a real `InstalledApplication`.

Risks:

- The shelf format is Microchip private state, not a documented extension interface.
- The scanner's matching rules are not schema-documented.
- App-finder cache updates may overwrite local changes.
- The patch couples CC5X support to Microchip's private app-discovery implementation.

Conclusion: possible, but brittle and not the first patch to build.

### Direct Descriptor Bypass Patch

Patch `registerCustomToolchain(path)` in `microchip.toolchains/dist/extension.js` so it special-cases CC5X before or after the normal scan:

1. Check the selected folder for `descriptor-mplab-opt.json`.
2. Parse the descriptor and require `id` or `name` to identify CC5X.
3. Locate the CC5X wrapper binary, preferably `bin/cc5x-mplab-wrapper`.
4. Construct Microchip's internal `InstalledApplication`:

```js
new InstalledApplication(
  { name: "cc5x", version: descriptor.version || "3.8C" },
  selectedFolder,
  "local",
  path.join(selectedFolder, "bin")
)
```

5. Call the existing internal registration path:

```js
await this.registerToolchains(cc5xApplication);
await this.registerProviders();
```

Why this is the best patch candidate:

- It bypasses app-finder only for CC5X.
- It reuses Microchip's existing descriptor reader.
- `ToolchainXC.getToolchainDescriptor()` already checks the installed folder for `descriptor-mplab-opt.json`.
- It does not require pretending CC5X is XC8.

Risks:

- The extension bundle is minified and Webpack-packed.
- `InstalledApplication` is an internal class, not a stable API.
- Every Microchip extension update can break or overwrite the patch.
- VS Code extension integrity and marketplace updates may replace the file.
- There is still a second-order risk that downstream project/build logic assumes XC/GCC semantics.

Conclusion: if we do a patch proof of concept, use direct descriptor bypass, not the app-finder shelf path.

## Private-Service Integration Route

### Public Exports Checked

Loading the extension bundles with a stubbed `vscode` module showed:

`microchip.mplab-extensions-core` exports:

```text
CorePlugin
activate
coreInsightsInstance
deactivate
```

`microchip.toolchains` exports:

```text
activate
deactivate
```

The following are not publicly exported:

- `Lookup`
- `ProviderService`
- `ToolchainService`
- `ProjectService`
- `BasePlugin`
- `ToolchainXC`
- `XCLanguageToolchainProvider`
- option-book provider classes

Conclusion: a separate extension cannot cleanly import the service registry or toolchain provider classes through the normal VS Code extension export mechanism.

### Manifest-Discovered Provider Possibility

The core bundle contains logic that scans extension manifests for Microchip provider declarations under:

```json
{
  "contributes": {
    "mplab": {
      "providers": []
    }
  }
}
```

This suggests a third-party extension might be discoverable as a Microchip provider if it advertises the right provider metadata.

However, provider lookup then needs the activated extension to return provider objects compatible with Microchip's private provider interfaces. Since the base classes and provider contracts are not public, a CC5X extension would have to reimplement the expected shape from bundle analysis.

Likely required shape:

- extension export object with initialized state
- `getProviders(providable, providerName, mode)` method
- provider object with Microchip's expected `providable` and `providerName`
- toolchain provider methods used by RunCMake/project property flows
- option descriptors compatible with Microchip's option book code

Risks:

- There is no stable TypeScript type package.
- There is no published compatibility promise.
- Method names and enum values are minified/private implementation details.
- A provider may be discoverable but still fail when RunCMake asks it for build metadata.

Conclusion: a private-service companion is research-grade only until Microchip exposes a supported provider SDK or the required provider shape is fully proven by a disposable extension.

## Recommended Experimental Sequence

### Phase P0: Probe Only

Keep this phase non-mutating.

Tasks:

- Locate installed Microchip extension versions.
- Verify `SUPPORTED_COMPILERS` anchors.
- Verify `registerCustomToolchain` anchor.
- Verify `InstalledApplication` constructor anchor.
- Verify extension exports.
- Validate the CC5X descriptor/build JSON against Microchip's schemas.

Deliverable:

- A repeatable probe script.

### Phase P1: Patch-Copy Prototype

Do not edit `~/.vscode/extensions` yet.

Tasks:

- Generate a patched copy of `dist/extension.js`.
- Inject a CC5X special-case into `registerCustomToolchain`.
- Keep the original extension untouched.
- Diff the generated copy and inspect the exact injected code.

Exit criteria:

- Patch applies cleanly to the current 1.7.3 bundle.
- Generated file still parses as JavaScript.
- Patch is small and anchored to stable nearby text.

### Phase P2: Disposable VS Code Profile Test

Use a throwaway VS Code profile/extensions directory.

Tasks:

- Copy the patched `microchip.toolchains` extension into the disposable profile.
- Install Microchip core dependencies in the same profile.
- Add a fake CC5X install folder containing:
  - `descriptor-mplab-opt.json`
  - `builds-mplab-opt.json`
  - `bin/cc5x-mplab-wrapper`
- Run `MPLAB: Add Toolchain`.
- Confirm whether `microchip.toolchains:cc5x@3.8C` appears in Microchip project properties.

Exit criteria:

- CC5X appears as a named toolchain.
- Descriptor provider loads.
- No activation crash in the extension host logs.

### Phase P3: Build Pipeline Test

Tasks:

- Generate a minimal `.mplab.json` using `microchip.toolchains:cc5x@3.8C`.
- Try Microchip RunCMake build with the CC5X provider selected.
- Capture the exact command line passed to `cc5x-mplab-wrapper`.
- Confirm diagnostics and `.hex` generation.

Exit criteria:

- The wrapper receives enough device/source context to call CC5X correctly.
- Microchip's build flow does not require GCC-only output assumptions for this simple case.

Abort if:

- CMake generation assumes GCC compiler identity.
- The build requires ELF/DWARF outputs that CC5X cannot produce.
- Device filtering prevents PIC10/PIC12/PIC16 CC5X devices from selecting the provider.

### Phase S1: Private Provider Skeleton

Only start this if the patch-copy path is too invasive but the provider discovery hook looks usable.

Tasks:

- Create a disposable extension with `contributes.mplab.providers`.
- Export a shape-compatible plugin object.
- Return a placeholder provider and verify Microchip core can discover it.
- Avoid any real project build logic until discovery is confirmed.

Exit criteria:

- Microchip core calls the extension's provider methods.
- Provider discovery happens without importing private bundle modules.

Abort if:

- Discovery requires private symbols from Microchip's Webpack bundle.
- Core rejects the provider due to `instanceof` checks against private classes.
- RunCMake assumes providers come only from `microchip.toolchains`.

## Production Decision

Do not ship either invasive route as the main implementation.

Production should remain:

- standalone `cc5x-vscode` extension
- CC5X-specific task provider
- diagnostics from the CC5X helper/wrapper
- Microchip programming/debug tasks generated only where they consume produced artifacts cleanly

Keep patching/private-service work under `experimental/` and label it unsupported.
