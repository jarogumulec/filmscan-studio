# FreeCAD Macro: M42x0.75 Camera Thread Bellows Mount

**AI Agent Reproducibility Documentation**

## Overview

This macro creates three SLA-fit variants of a camera-side bellows adapter. Each adapter is a 58 mm diameter mounting ring with an upward-facing male M42x0.75 thread, intended for a camera with a female M42x0.75 interface such as the ToupTek ATR series.

---

## Specifications

| Parameter | Value | Unit | Notes |
|-----------|-------|------|-------|
| Ring Outer Diameter | 58.0 | mm | Flat mounting-ring diameter |
| Ring Height | 2.5 | mm | Base-ring thickness |
| Centering Collar Height | 0.5 | mm | Unthreaded collar above the ring |
| Thread Standard | M42x0.75 | - | Male ISO metric thread |
| Nominal Thread Diameter | 42.0 | mm | Major diameter before SLA allowance |
| Thread Pitch | 0.75 | mm | Metric thread pitch |
| Thread Length | 9.4 | mm | Threaded height above the collar |
| Thread Profile | 60.0 | degrees | ISO metric profile |
| Thread Wall Thickness | 1.5 | mm | Supporting tube thickness below the external thread |
| Clear Optical Aperture | 38.0 | mm approx. | Derived from the thread root less the supporting wall |
| Thread Trim Tolerance | 0.01 | mm | Prevents zero-thickness faces at the thread ends |
| Thread Ridge Overlap | 0.05 | mm | Volumetric overlap joining the thread ridge to its support tube |
| Bolt-Hole Diameter | 2.0 | mm | Four through-holes |
| Counterbore Diameter | 3.0 | mm | Screw-head recess, top side |
| Counterbore Depth | 1.3 | mm | From the top surface |
| Bolt-Hole PCD | 51.54 | mm | Hole-center circle |
| Bolt Pattern | Square | - | Side approximately 36.44 mm |
| Tight Allowance | -0.05 | mm radial | Largest male thread for SLA test |
| Standard Allowance | -0.10 | mm radial | Recommended first SLA print |
| Loose Allowance | -0.15 | mm radial | Smallest male thread for easier fitting |

## Geometric Construction Logic

1. Create a 58.0 mm diameter, 2.5 mm high base ring with a central through-opening.
2. Add a 0.5 mm high unthreaded centering collar at the thread root.
3. Add a 1.5 mm thick supporting tube under the thread, retaining an approximately 38.0 mm clear optical aperture.
4. Sweep a 60-degree triangular profile along a native helix to create a 9.4 mm long male M42x0.75 thread above the collar.
5. Cut four Ø2.0 mm through-holes at 45, 135, 225, and 315 degrees on the Ø51.54 mm pitch circle.
6. Cut four coaxial Ø3.0 mm by 1.3 mm deep counterbores from the top side, which is also the thread side.

## Execution Instructions

1. In FreeCAD choose **Macro -> Macros...**.
2. Execute `camera_thread_m42_bellows.FCMacro`.
3. Export the desired `Tight`, `Standard`, or `Loose` object as STL.

## Parametric Modification

The `THREAD_LENGTH = 9.4` parameter controls only the threaded section. The total part height is 12.4 mm: 2.5 mm base ring + 0.5 mm collar + 9.4 mm thread.

`FIT_VARIANTS` stores radial offsets for the external thread. A negative value reduces the male thread size for SLA fit clearance.

## Validation Criteria

- [ ] Three objects appear in the Model tree.
- [ ] Each object has a 58.0 mm outside diameter and a 2.5 mm high base ring.
- [ ] The male M42x0.75 thread rises 9.4 mm above a 0.5 mm collar.
- [ ] A 1.5 mm thick tube supports the thread while the optical opening remains approximately Ø38.0 mm.
- [ ] Four Ø2.0 mm through-holes form a square on a Ø51.54 mm PCD.
- [ ] Each hole has a Ø3.0 mm by 1.3 mm deep top-side counterbore.
- [ ] `Shape.isValid()` returns `True` for every variant.

## 3D Printing Notes

- Target printer: Prusa SL1S SLA resin printer.
- Print with the threaded and counterbored face upward when feasible, using supports on the opposite face.
- Wash uncured resin from the thread before post-curing and fit testing.
- Print the Standard variant first; use Tight only for excess play and Loose if the camera thread binds.

## Common Issues & Solutions

| Issue | Cause | Solution |
|-------|-------|----------|
| Camera thread binds | Resin growth or residue | Clean thoroughly and try the Loose variant |
| Camera thread has play | Excess clearance | Try the Tight variant |
| Screw heads protrude | Screw head is wider than 3 mm | Use suitable screws or increase `COUNTERBORE_DIAMETER` |

## AI Agent Notes

- This is the camera-side counterpart to `objective_thread_m39_bellows.FCMacro`.
- The thread is a real helical solid suitable for STL export.
- Keep this documentation paired with its macro in the repository root.

## Version History

| Date | Version | Changes |
|------|---------|---------|
| 2026-09-16 | 1.0 | Initial M42x0.75 male-thread camera-side bellows mount |