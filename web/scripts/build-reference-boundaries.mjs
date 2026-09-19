import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// All geometry is retained from the pinned public-domain Natural Earth release.
const input = process.argv[2];
if (!input) throw new Error('Provide the directory containing the four pinned Natural Earth GeoJSON files.');
const output = resolve(dirname(fileURLToPath(import.meta.url)), '../src/data');
const sources = [
  ['land', 'ne_10m_admin_0_boundary_lines_land'],
  ['claims', 'ne_10m_admin_0_boundary_lines_disputed_areas'],
  ['maritime', 'ne_10m_admin_0_boundary_lines_maritime_indicator'],
  ['china-maritime', 'ne_10m_admin_0_boundary_lines_maritime_indicator_chn'],
];
const expected = JSON.parse(readFileSync(join(output, 'reference-boundaries.provenance.json'), 'utf8')).sources;
const disputedClasses = new Set([
  'Disputed (please verify)', 'Indefinite (please verify)', 'Line of control (please verify)',
  'Indeterminant frontier', 'Unrecognized', 'Claim boundary', 'Breakaway',
  'Elusive frontier', 'Marine Indicator Disputed',
]);
const historyClasses = new Set(['Reference line', 'Overlay limit', 'Lease limit']);
// Four source-tagged CN/TW exclusions plus the legacy offshore arc east of Taiwan.
// That arc has no CN override and is not part of the publisher's China supplement;
// do not repurpose it as an additional claim stroke.
const maritimeChinaIds = [1746707341, 1746707349, 1746707357, 1746707365, 1746707375];
// This old Doklam line has Bhutan on both sides. It is inside the same release's
// CHN worldview polygon; its FCLASS_CN is Unrecognized. Retaining it would leave
// a second line beside the replacement Chinese-claim feature 1746705373.
const additionalChinaIds = [1746708771];
const features = [];
const provenance = [];
for (const [kind, name] of sources) {
  const bytes = readFileSync(join(input, `${name}.geojson`));
  const sha256 = createHash('sha256').update(bytes).digest('hex');
  const recorded = expected.find(s => s.name === `${name}.geojson`);
  if (!recorded || sha256 !== recorded.sha256) throw new Error(`Source checksum mismatch: ${name}`);
  provenance.push({ ...recorded, kind });
  for (const [sourceIndex, feature] of JSON.parse(bytes).features.entries()) {
    const p = feature.properties;
    const parties = [p.ADM0_A3_L, p.ADM0_A3_R, p.SOV_A3_L, p.SOV_A3_R].filter(Boolean);
    const description = [p.NAME, p.NOTE, p.COMMENT, p.ADM0_LEFT, p.ADM0_RIGHT].filter(Boolean).join(' ');
    const chinaRelated = kind === 'china-maritime' ||
      parties.some(code => ['CHN', 'TWN', 'HKG', 'MAC'].includes(code)) ||
      /\b(china|chinese|taiwan)\b/i.test(description) ||
      maritimeChinaIds.includes(p.ne_id) || additionalChinaIds.includes(p.ne_id);
    const cnOverride = chinaRelated && typeof p.FCLASS_CN === 'string' && p.FCLASS_CN.length > 0;
    const viewClass = cnOverride ? p.FCLASS_CN : p.FEATURECLA;
    // Unclassified claim-only lines are not promoted to borders. In particular,
    // 1746705607 is absent from the matching CHN worldview polygon boundary.
    const display = p.ne_id !== 1746707375 && !historyClasses.has(viewClass) &&
      !(chinaRelated && ['Unrecognized', 'Claim boundary'].includes(viewClass));
    features.push({
      // ne_id is not unique even within the publisher's land layer.
      type: 'Feature', id: `${kind}:${sourceIndex}:${p.ne_id}`,
      properties: {
        source_id: p.ne_id, kind, source_class: p.FEATURECLA,
        view_class: viewClass, classification: cnOverride ? 'source-cn' : 'source-default',
        disputed: disputedClasses.has(viewClass), china_related: chinaRelated, display,
        internal: chinaRelated && ['Map unit boundary', 'Admin-1 boundary'].includes(viewClass),
        maritime_indicator: chinaRelated && ['maritime', 'china-maritime'].includes(kind),
      },
      geometry: feature.geometry,
    });
  }
}
const metadata = {
  policy: 'china-viewpoint-reference-lines-v2',
  license: 'Public domain', release: 'Natural Earth v5.1.2', scale: '1:10,000,000',
  sources: provenance, manual_maritime_ids: maritimeChinaIds,
  additional_china_ids: additionalChinaIds,
  hidden_feature_ids: features.filter(f => !f.properties.display && f.properties.china_related).map(f => f.properties.source_id),
  replacement_feature_ids: features.filter(f => f.properties.display && f.properties.china_related &&
    disputedClasses.has(f.properties.source_class) && !f.properties.disputed).map(f => f.properties.source_id),
  maritime_indicator_ids: features.filter(f => f.properties.display && f.properties.maritime_indicator).map(f => f.properties.source_id),
  feature_count: features.length,
  limitations: 'China-related features use the source CN classification; other regions retain the default classification. Original coordinates are unchanged. The nine maritime strokes come from the China supplement. The legacy Taiwan-east arc has no CN override and is omitted; it is not repurposed as an additional claim stroke. Unclassified claim-only and historical lines are omitted. Small-scale visual reference, independent of DEM and manufacturing coordinates.',
};
writeFileSync(join(output, 'reference-boundaries.json'), JSON.stringify({ type: 'FeatureCollection', features })+'\n');
writeFileSync(join(output, 'reference-boundaries.provenance.json'), JSON.stringify(metadata, null, 2)+'\n');
console.log(JSON.stringify({features:features.length, replacements:metadata.replacement_feature_ids.length,
  maritimeIndicators:metadata.maritime_indicator_ids.length, hidden:metadata.hidden_feature_ids.length}));
