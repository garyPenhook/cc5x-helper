# TB3261 Considerations For `cc5x_setcc_native`

Source considered: [getting_started_C.pdf](/home/gary/apps/cc5x_paid/getting_started_C.pdf)

This PDF is Microchip technical brief `TB3261`, titled `PIC1000: Getting Started with Writing C-Code for PIC16 and PIC18`, dated 2020.

## Why It Matters

This brief is not a CC5X specification and it is not a replacement for BKND header behavior. It is, however, a strong reference for:

- modern Microchip register and bit naming conventions
- modern device-header structure expectations
- multibyte register naming (`REG`, `REGL`, `REGH`)
- short-name versus long-name bit naming
- configuration-bit workflow expectations
- legacy-peripheral naming exceptions

That makes it directly relevant to the pack-first reimplementation.

## Key Takeaways

### 1. Modern PIC headers expose multiple layers of names

TB3261 describes device headers as containing:

- register definitions
- bit masks
- bit-field masks
- bit positions
- register unions with short and long bit names

For `cc5x_setcc_native`, this means the normalized device model must preserve more than one naming layer where the pack metadata provides it.

For CC5X header generation, we should map these into:

- register declarations
- optional register aliases
- bit declarations
- optional alternate bit declarations

### 2. Long and short bit names are both intentional

The brief explicitly shows modern headers exposing both:

- long names such as `ADGO`
- short compatibility names such as `GO`

This supports the current generator rule that maps raw pack names like `GOnDONE` into CC5X-facing aliases such as:

- `ADGO`
- `GO`

This is not arbitrary convenience. It matches Microchip’s documented naming style.

### 3. Legacy peripherals are exceptions by design

TB3261 explicitly calls out legacy peripherals that do not strictly follow the newer naming convention, including:

- EUSART
- MSSP

This is important because many of the awkward alias and bit-name cases seen in shipped CC5X headers are concentrated exactly there.

Implementation consequence:

- alias suppression and alternate-name emission cannot be purely generic
- EUSART and MSSP should be treated as special-case module families in the compatibility layer

### 4. Multibyte registers should remain explicit

The brief documents the `REG`, `REGL`, `REGH` convention for multibyte registers.

Implementation consequence:

- the normalized model should continue preserving 16-bit parent register names
- the header generator should decide explicitly when to emit only `L/H` byte names, when to emit the 16-bit parent alias, and when to suppress the parent for CC5X parity

### 5. Configuration bits must be fully specified

TB3261 is very explicit that device configuration is fundamental and that every configuration setting should be specified explicitly rather than left implicit.

Implementation consequence:

- pack-native config rendering should support `--include-defaults`
- project workflows should prefer complete generated config blocks
- diagnostics should encourage explicit full-device config rather than partial ad hoc settings

### 6. Modern Microchip headers are macro/union based, but CC5X is not

TB3261’s header examples are for XC8-style headers using:

- `#include <xc.h>`
- register macros
- unions/bitfields
- mask macros

CC5X uses a different header model:

- `char REG @ ...;`
- `bit NAME @ REG.n;`
- `#pragma chip`
- `#pragma config`

Implementation consequence:

- we should use TB3261 to understand naming intent
- we should not mimic XC8 header mechanics directly
- translation into CC5X idioms must be deliberate

## Practical Rules To Carry Forward

1. Treat pack metadata as the source of truth for new devices.
2. Treat TB3261 as the source of truth for modern Microchip naming intent.
3. Treat shipped BKND headers as compatibility samples only where they exist.
4. Maintain a CC5X compatibility layer that can:
   - suppress noisy raw pack aliases
   - emit selected compatibility aliases
   - rename specific bit names into CC5X-friendly forms
5. Handle legacy-peripheral families, especially EUSART and MSSP, with explicit rules rather than generic heuristics.
6. Prefer complete config emission for reproducible behavior.

## Immediate Follow-Up Work Suggested By TB3261

- Add module-family-aware alias policies for:
  - EUSART
  - MSSP
- Distinguish canonical names, short names, and compatibility aliases in the normalized bit model.
- Add an output mode that can emit:
  - canonical-only bits
  - canonical plus compatibility aliases
- Extend config workflows so project-level generation defaults to explicit full configuration.
