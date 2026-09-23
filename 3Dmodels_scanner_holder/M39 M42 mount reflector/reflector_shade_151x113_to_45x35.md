# FreeCAD Macro: Reflector Shade 151x113 to 45x35mm

**AI Agent Reproducibility Documentation**

## Overview

This macro generates a hollow rectangular truncated-pyramid shade for an illumination reflector. The light is mounted at the large base opening, while the opposite opening narrows to 45 x 35 mm. The narrow opening is offset by 16 mm along the short axis, so its center is not coaxial with the base opening.

## Specifications

| Parameter | Value | Unit | Notes |
|-----------|-------|------|-------|
| Inner base opening | 151 x 113 | mm | Large opening, light side |
| Inner top opening | 45 x 35 | mm | Narrow opening |
| Height | 130.0 | mm | Distance between opening planes |
| Wall thickness | 2.0 | mm | Nominal wall thickness |
| Top center offset | 16.0 | mm | Along the short axis (+Y) |
| Wide-side extension | 4.0 | mm | Hollow rectangular continuation beyond the base |
| Small-side extension | 5.0 | mm | Hollow rectangular continuation beyond the narrow opening |
| Base flange overhang | 2.0 | mm | Extends outward from the wide-side outer dimensions on every side |
| Base flange height | 2.0 | mm | Square-edged perimeter flange below the wide-side extension |
| Outer base size | 155 x 117 | mm | Inner size + 2 x wall thickness |
| Outer top size | 49 x 39 | mm | Inner size + 2 x wall thickness |
| Coordinate system | X long, Y short, Z height | - | Base center at (0, 0, 0) |

## Geometric Construction Logic

1. Create an outer rectangle of 155 x 117 mm at `z = -4`, centered at `y = 0`.
2. Create an outer rectangle of 155 x 117 mm at `z = 0`, centered at `y = 0`.
3. Create an outer rectangle of 49 x 39 mm at `z = 130`, centered at `y = 16`.
4. Create an outer rectangle of 49 x 39 mm at `z = 135`, centered at `y = 16`.
5. Loft these profiles into the outside of the shade, including the 4 mm and 5 mm hollow extensions.
6. Create matching inner profiles of 151 x 113 mm and 45 x 35 mm at the same four heights.
7. Loft the inner profiles into a cutting solid.
8. Subtract the inner loft from the outer loft. The result is open at both ends and has a nominal 2 mm wall.
9. Create a 2 mm high rectangular perimeter ring below the wide-side extension. Its outside dimensions are 159 x 121 mm and its clear opening remains 151 x 113 mm.
10. Fuse the perimeter ring to the reflector body.

The offset is applied to both the outer and inner top profiles. Therefore, the wall follows the same slanted centerline and the inner dimensions remain the specified clear dimensions.

## FreeCAD Python API Sequence

| Step | API Method | Purpose |
|------|------------|---------|
| 1 | `Base.Vector(x, y, z)` | Define rectangle vertices |
| 2 | `Part.makePolygon(points)` | Create closed rectangular profile wires |
| 3 | `Part.makeLoft([wire1, wire2], True, False)` | Create outer and inner solids |
| 4 | `shape.cut(other_shape)` | Hollow the outer loft |
| 5 | `doc.addObject("Part::Feature", name)` | Insert the final shape |
| 6 | `doc.recompute()` | Refresh the FreeCAD document |

## Execution Instructions

### FreeCAD GUI

1. Open FreeCAD.
2. Navigate to **Macro -> Macros...**.
3. Select `reflector_shade_151x113_to_45x35.FCMacro`.
4. Click **Execute**.

### FreeCAD Python Console

```python
exec(open("/path/to/reflector_shade_151x113_to_45x35.FCMacro").read())
```

### Command Line

```bash
freecad -c reflector_shade_151x113_to_45x35.FCMacro
```

## Parametric Modification

The editable dimensions are at the top of the macro:

```python
INNER_BASE_LENGTH = 151.0
INNER_BASE_WIDTH = 113.0
INNER_TOP_LENGTH = 45.0
INNER_TOP_WIDTH = 35.0
HEIGHT = 130.0
WALL_THICKNESS = 2.0
TOP_OFFSET_SHORT_AXIS = 16.0
BASE_EXTENSION_LENGTH = 4.0
TOP_EXTENSION_LENGTH = 5.0
BASE_FLANGE_WIDTH = 2.0
BASE_FLANGE_HEIGHT = 2.0
```

Set `TOP_OFFSET_SHORT_AXIS = -16.0` to reverse the offset direction. Set it to `0.0` for a coaxial shade.

## Validation Criteria

- The Model tree contains `ReflectorShade_151x113_to_45x35`.
- The shape is a single valid solid and is open at both rectangular ends.
- The base clear opening measures 151 x 113 mm.
- The narrow clear opening measures 45 x 35 mm.
- The height measures 130 mm.
- The narrow opening center is displaced by 16 mm in the short axis.
- The nominal wall thickness is 2 mm.

Programmatic check:

```python
obj = FreeCAD.ActiveDocument.ReflectorShade_151x113_to_45x35
print(obj.Shape.isValid())
print(obj.Shape.Solids)
print(obj.Shape.Volume)
```

## Common Issues & Solutions

| Issue | Cause | Solution |
|-------|-------|----------|
| Opening appears on the wrong side | Offset sign is reversed | Change `TOP_OFFSET_SHORT_AXIS` from `16.0` to `-16.0` |
| Shade is not hollow | Loft or cut failed | Confirm both opening dimensions exceed twice the wall thickness |
| Invalid shape | Profile dimensions or height are non-positive | Check the parameter validation section |
| Top opening is not centered as expected | Offset was measured in the wrong axis | The long axis is X and the short axis is Y |

## AI Agent Notes

- Paradigm: constructive solid geometry using two lofts and one cut.
- Units: millimeters.
- Base opening is the light side.
- The macro intentionally keeps the top profile parallel to the base profile and translates it only in Y.
- The wide-side extension continues from `z = 0` to `z = -4`; the small-side extension continues from `z = 130` to `z = 135`.
- The square-edged base flange occupies `z = -6` to `z = -4`, overhangs the wide-side outer dimensions by 2 mm on every side, and keeps the 151 x 113 mm clear opening.
- Wall thickness is specified by offsetting each rectangular profile by 2 mm in X and Y; on sloped faces this is nominal rather than a mathematically constant normal thickness.

## Version History

| Date | Version | Changes |
|------|---------|---------|
| 2026-09-16 | 1.0 | Initial offset reflector shade model |
