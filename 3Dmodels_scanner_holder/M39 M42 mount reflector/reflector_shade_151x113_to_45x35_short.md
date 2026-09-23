# FreeCAD Macro: Reflector Shade 151x113 to 45x35mm Short

**AI Agent Reproducibility Documentation**

## Overview

This is the short variant of the hollow rectangular truncated-pyramid illumination reflector shade. It keeps the same openings, 16 mm short-axis offset, wall thickness, end extensions, and base flange as the standard variant, but reduces the main tapered height from 130 mm to 70 mm.

## Specifications

| Parameter | Value | Unit | Notes |
|-----------|-------|------|-------|
| Inner base opening | 151 x 113 | mm | Large opening, light side |
| Inner top opening | 45 x 35 | mm | Narrow opening |
| Main height | 70.0 | mm | Distance between opening planes |
| Wall thickness | 2.0 | mm | Nominal wall thickness |
| Top center offset | 16.0 | mm | Along the short axis (+Y) |
| Wide-side extension | 4.0 | mm | Hollow rectangular continuation below the base |
| Small-side extension | 5.0 | mm | Hollow rectangular continuation above the narrow opening |
| Base flange overhang | 2.0 | mm | Extends outward on every side |
| Base flange height | 2.0 | mm | Square-edged perimeter flange |
| Outer base size | 155 x 117 | mm | Inner size + 2 x wall thickness |
| Base flange outside size | 159 x 121 | mm | Outer base size + 2 x flange overhang |
| Coordinate system | X long, Y short, Z height | - | Base center at (0, 0, 0) |

## Geometric Construction Logic

1. Create outer profiles of 155 x 117 mm at `z = -4` and `z = 0`, centered at `y = 0`.
2. Create outer profiles of 49 x 39 mm at `z = 70` and `z = 75`, centered at `y = 16`.
3. Loft the four outer profiles into the outside of the shade.
4. Create matching inner profiles of 151 x 113 mm and 45 x 35 mm at the same four heights.
5. Loft the inner profiles into a cutting solid.
6. Subtract the inner loft from the outer loft. The result is open at both ends and has a nominal 2 mm wall.
7. Create a 2 mm high rectangular perimeter ring from `z = -6` to `z = -4`. Its outside dimensions are 159 x 121 mm and its clear opening remains 151 x 113 mm.
8. Fuse the perimeter ring to the reflector body.

The offset is applied to both the outer and inner top profiles. The short variant changes only the main height from 130 mm to 70 mm.

## FreeCAD Python API Sequence

| Step | API Method | Purpose |
|------|------------|---------|
| 1 | `Base.Vector(x, y, z)` | Define rectangle vertices |
| 2 | `Part.makePolygon(points)` | Create closed rectangular profile wires |
| 3 | `Part.makeLoft([wire1, wire2, ...], True, False)` | Create outer and inner solids |
| 4 | `shape.cut(other_shape)` | Hollow the outer loft |
| 5 | `Part.makeBox(length, width, height, base)` | Create the base perimeter flange |
| 6 | `shape.fuse(other_shape)` | Join the flange to the reflector body |
| 7 | `doc.recompute()` | Refresh the FreeCAD document |

## Execution Instructions

### FreeCAD GUI

1. Open FreeCAD.
2. Navigate to **Macro -> Macros...**.
3. Select `reflector_shade_151x113_to_45x35_short.FCMacro`.
4. Click **Execute**.

### FreeCAD Python Console

```python
exec(open("/path/to/reflector_shade_151x113_to_45x35_short.FCMacro").read())
```

### Command Line

```bash
freecad -c reflector_shade_151x113_to_45x35_short.FCMacro
```

## Parametric Modification

The editable dimensions are at the top of the macro:

```python
INNER_BASE_LENGTH = 151.0
INNER_BASE_WIDTH = 113.0
INNER_TOP_LENGTH = 45.0
INNER_TOP_WIDTH = 35.0
HEIGHT = 70.0
WALL_THICKNESS = 2.0
TOP_OFFSET_SHORT_AXIS = 16.0
BASE_EXTENSION_LENGTH = 4.0
TOP_EXTENSION_LENGTH = 5.0
BASE_FLANGE_WIDTH = 2.0
BASE_FLANGE_HEIGHT = 2.0
```

Set `TOP_OFFSET_SHORT_AXIS = -16.0` to reverse the offset direction. Set it to `0.0` for a coaxial shade.

## Validation Criteria

- The Model tree contains `ReflectorShadeShort_151x113_to_45x35`.
- The shape is a single valid solid and is open at both rectangular ends.
- The base clear opening measures 151 x 113 mm.
- The narrow clear opening measures 45 x 35 mm.
- The main tapered height measures 70 mm.
- The total height including extensions and flange is 86 mm, from `z = -6` to `z = 80`.
- The narrow opening center is displaced by 16 mm in the short axis.
- The nominal wall thickness is 2 mm.

## Common Issues & Solutions

| Issue | Cause | Solution |
|-------|-------|----------|
| Opening appears on the wrong side | Offset sign is reversed | Change `TOP_OFFSET_SHORT_AXIS` from `16.0` to `-16.0` |
| Shade is not hollow | Loft or cut failed | Confirm both opening dimensions exceed twice the wall thickness |
| Invalid shape | Profile dimensions or height are non-positive | Check the parameter validation section |
| Top opening is not centered as expected | Offset was measured in the wrong axis | The long axis is X and the short axis is Y |

## AI Agent Notes

- This is a separate short variant; the standard 130 mm macro is unchanged.
- Paradigm: constructive solid geometry using two lofts, one cut, one flange cut, and one fuse.
- Units: millimeters.
- Base opening is the light side.
- The top profile remains parallel to the base profile and translates only in Y.
- The square-edged base flange occupies `z = -6` to `z = -4`.
- The short variant uses `z = 70` for the narrow opening and `z = 75` after the 5 mm top extension.
- Wall thickness is specified by offsetting each rectangular profile by 2 mm in X and Y; on sloped faces this is nominal rather than a mathematically constant normal thickness.

## Version History

| Date | Version | Changes |
|------|---------|---------|
| 2026-09-16 | 1.0 | Initial 70 mm short variant |
