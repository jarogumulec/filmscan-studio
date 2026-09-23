# FreeCAD Macro: M39x1 Objective Thread Bellows Mount

**AI Agent Reproducibility Documentation**

## Overview

This macro creates three 5 mm high mounting rings for attaching a Leica-style M39x1 enlarger objective to a bellows assembly. Each ring has the same outer geometry and three through-holes, with a different radial allowance in its internal thread for SLA print fitting trials.

---

## Specifications

| Parameter | Value | Unit | Notes |
|-----------|-------|------|-------|
| Outer Diameter | 57.0 | mm | Flat ring outside diameter |
| Ring Height | 5.0 | mm | Also threaded length |
| Thread Standard | M39x1 | - | Nominal major diameter 39.0 mm; pitch 1.0 mm |
| Thread Flank Angle | 60.0 | degrees | ISO metric thread profile |
| Nominal Thread Depth | 0.6134 | mm | Basic ISO metric radial thread depth |
| Thread Length | 5.0 | mm | Five complete pitches through the ring |
| Bolt-Hole Diameter | 2.0 | mm | Three through-holes |
| Counterbore Diameter | 4.0 | mm | Recess for each screw head, from the top face |
| Counterbore Depth | 1.5 | mm | Recess depth from the top face |
| Bolt-Hole Pitch-Circle Diameter | 51.38 | mm | Hole-center circle |
| Hole A Angle | 105.0 | degrees | Zero degrees is +X; 105 degrees is approximately 12:30 |
| Hole A-B Chord | 39.23 | mm | Derived from selected angular placement |
| Hole A-C Chord | 46.70 | mm | Derived from selected angular placement |
| Hole B-C Chord | 46.53 | mm | Derived from selected angular placement |
| Tight Variant Allowance | 0.05 | mm | Radial clearance, suitable as the first SLA test |
| Standard Variant Allowance | 0.10 | mm | Radial clearance, recommended initial SLA fit |
| Loose Variant Allowance | 0.15 | mm | Radial clearance, easier-to-start thread |

## Geometric Construction Logic

1. Create a solid cylinder with an outside radius of 28.5 mm and height of 5.0 mm.
2. For each fit variant, create a central major-diameter bore and fuse a swept 60-degree triangular helical ridge facing into the bore. The nominal M39x1 profile is offset radially by that variant's allowance.
3. Calculate hole positions from the 51.38 mm pitch-circle diameter. Hole A is fixed at 105 degrees; B and C are calculated from the supplied chord lengths, preserving the specified non-uniform pattern.
4. Subtract three 2.0 mm diameter cylinders through the full ring height, each with a coaxial 4.0 mm diameter by 1.5 mm deep counterbore from the top face.
5. Add all three finished solids to the active FreeCAD document as separate Part features.

### Hole Coordinates

The exact chord lengths determine the angular separations. With A set at 105 degrees, the resulting approximate polar positions are:

| Hole | Angle | Clock Position (approx.) |
|------|-------|--------------------------|
| A | 105.0 degrees | 12:30 |
| B | 5.4 degrees | 12:01 |
| C | 235.7 degrees | 7:51 |

The hour positions are only orientation aids. The macro preserves the three supplied chord distances rather than the rough clock descriptions.

## FreeCAD Python API Sequence

| Step | API Method | Purpose |
|------|------------|---------|
| 1 | `Part.makeCylinder()` | Create the blank ring, bore, bolt-hole, and counterbore cutters |
| 2 | `Part.makePolygon()` | Create successive triangular profiles along the M39x1 helix |
| 3 | `Part.makeLoft()` | Loft the profiles into the triangular ISO metric thread cutter |
| 4 | `shape.cut()` and `shape.fuse()` | Create the major bore, fuse the internal thread ridge, and subtract bolt holes |
| 5 | `doc.addObject()` | Add each fit variant to the document |

## Execution Instructions

1. Open FreeCAD and choose **Macro -> Macros...**.
2. Select `objective_thread_m39_bellows.FCMacro` and execute it.
3. The Model tree will contain `M39x1_BellowsMount_Tight`, `M39x1_BellowsMount_Standard`, and `M39x1_BellowsMount_Loose`.
4. Export only the desired variant as STL for printing.

## Parametric Modification

All editable dimensions are in the macro's `PARAMETERS` section. To change an SLA fitting trial, edit the corresponding value in `FIT_VARIANTS`:

```python
FIT_VARIANTS = [
    ("Tight", 0.05),
    ("Standard", 0.10),
    ("Loose", 0.15),
]
```

Use a radial, not diametral, value. For example, increasing the allowance by 0.05 mm increases the thread major diameter by 0.10 mm.

## Validation Criteria

- [ ] Three objects appear in the Model tree, one for each clearance variant.
- [ ] `Shape.isValid()` returns `True` for all three objects.
- [ ] Each ring measures 57.0 mm outside diameter and 5.0 mm height.
- [ ] The M39x1 thread runs continuously through the entire 5.0 mm height.
- [ ] The three bolt holes measure 2.0 mm diameter and lie on a 51.38 mm pitch circle.
- [ ] Each bolt hole has a 4.0 mm diameter, 1.5 mm deep counterbore on the top face.
- [ ] Measured center distances are AB = 39.23 mm, AC = 46.70 mm, and BC = 46.53 mm.

## 3D Printing Notes

- Target printer: Prusa SL1S SLA resin printer.
- Print the ring flat on its largest face, with supports only where required by the chosen resin and build plate preparation.
- Clean uncured resin from the thread with the manufacturer's recommended wash process before post-curing; residue changes the fit substantially.
- Test the `Standard` variant first. Use `Tight` only if the standard fit has noticeable backlash, or `Loose` if starting the objective thread requires force.
- Do not force the objective into a thread that binds; resin threads can chip. Verify fit before final assembly.

## Common Issues & Solutions

| Issue | Cause | Solution |
|-------|-------|----------|
| Objective will not start | Resin expansion or residual uncured resin | Clean the thread and try the Loose variant |
| Objective has excessive play | Clearance is too large | Try the Tight variant |
| Thread chips during fitting | Excessive force or incomplete cleaning | Stop fitting, clean the thread, and use the next looser variant |
| Holes do not align with bellows | Ring orientation is incorrect | Use hole A as the 12:30 orientation datum |

## AI Agent Notes

- The macro intentionally creates three separate solids for direct fit comparison.
- The internal thread is a real swept helical form suitable for STL export.
- Keep the macro and this documentation file paired in the repository root.

## Version History

| Date | Version | Changes |
|------|---------|---------|
| 2026-09-16 | 1.0 | Initial three-variant M39x1 bellows mount |