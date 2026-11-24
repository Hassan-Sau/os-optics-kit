# Catalog 3.6 conventions in prescriptions

The kit was checked offline against a locally built catalog 3.6 release (`2026-11-24.1`). These are the conventions
every case and every new prescription follows; they were established with three cases (`achromatic-imaging`,
`mirror-alignment`, `midwave-infrared-imager`) and then applied to all eighteen.

## Glass names: reference the catalogue material id

A bare designation is not unique. The 3.6 catalogue carries manufacturer-specific records (`CDGM_F2`, `SCHOTT_...`)
and, for many glasses, a separate record under the plain designation. `F2` is the documented example: the bare name
can be read as the CDGM glass or as the SCHOTT glass, and the two are different melts with different coefficients.
A prescription therefore names the **catalogue material id**, never only the trade designation:

```
"media": ["CDGM_F2"]        # authoritative: the catalogue record
"media": ["F2"]             # ambiguous: refuse, and choose an id
```

The id is what the record carries in `material_id`; the designation, aliases and maker stay in the record's
`identity` block for display. When a case's expected values are re-recorded, the id it names is part of the reason
for the change.

## Wavelengths: state vacuum or air

A 3.6 material states its own convention in `index_reference`: which wavelength the formula takes (air or vacuum),
at which temperature, and whether the index is relative to air. Prescriptions must not rely on a default. Two
consequences:

- A wavelength column is declared vacuum or air together with its source, so a band written from a catalogue
  (vacuum wavelengths) is not silently reused as an air band.
- The 0.14 engine always lowers the project's environment air for the ambient medium (an air material at the stated
  temperature and pressure, index about 1.000275 at 20 °C and 101325 Pa). A project that states no environment gets
  standard air. Under the previous engine the ambient medium was exactly vacuum. This alone moved a case: the
  `beam-expander` waist reads 1.99607 mm with air of index 1 and 1.95 mm at standard air; a copy of the case with
  `environment.pressure_pa = 0` reproduces the older value exactly. Cases state their environment explicitly.

## Unknown bulk loss: report a bound, never a number

The 3.6 catalogue carries a measured spectral `bulk_loss` for a minority of materials; most records have none. A
material without measured loss does not mean "no absorption". The engine reports the power that passes through such
a path as a bound and names the reason (`BULK_LOSS_NOT_ESTABLISHED`): the quantity's state is `BOUNDED`, the value is
an interval (for example 0 W to 8.5 mW at the `aspheric-lens` image plane), and a target on it stays outside its
model rather than being assessed. Cases therefore

- never compare such a value with a tolerance, and never record one as if it were exact;
- state the interval and the reason where the case reports the quantity;
- treat a `PARTIAL` execution that names this reason as expected, not as a failure.

## Result of the pass

Against os-optics 0.14.0 and catalog `2026-11-24.1`: every target outcome of the three probed cases is unchanged;
`achromatic-imaging` and `mirror-alignment` differ only in the recorded engine input hash, and
`midwave-infrared-imager` moves from `PARTIAL` to `COMPLETED` (the glass path is now computed). Across all eighteen
cases the same pattern holds: the only moved values are the three glass cases' execution status plus the recorded
input hash, and cases that pin the older catalogue release need a catalogue-pin update. No tolerance was relaxed.