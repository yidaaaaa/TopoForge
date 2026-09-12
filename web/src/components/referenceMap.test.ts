import { createExpression } from '@maplibre/maplibre-gl-style-spec';
import { describe, expect, it } from 'vitest';
import data from '../data/reference-boundaries.json';
import provenance from '../data/reference-boundaries.provenance.json';
import { boundaryVisibilityFilter, referenceBoundaryLayers, referenceLayers } from './referenceMap';

function evaluate(expression: unknown, properties: Record<string, unknown>): unknown {
  const parsed = createExpression(expression);
  if (parsed.result === 'error') throw new Error(JSON.stringify(parsed.value));
  return parsed.value.evaluate({ zoom: 4 }, { type: 2, properties });
}
function renderedLayers(id: number): string[] {
  const feature = data.features.find(f => f.properties.source_id === id);
  if (!feature) throw new Error(`Missing source feature ${id}`);
  return referenceBoundaryLayers().filter(layer => 'filter' in layer && evaluate(layer.filter, feature.properties) === true).map(layer => layer.id);
}

describe('China-viewpoint reference boundaries', () => {
  it('renders replacement claim geometry and the western border instead of removing every disputed line', () => {
    // Arunachal Pradesh, Aksai Chin, Doklam, and the China/Pakistan frontier.
    for (const id of [1746705401, 1746705405, 1746705409, 1746705431, 1746705373, 1746705535, 1746705543, 1746705319, 1746706305, 1746708661]) {
      expect(renderedLayers(id)).toEqual(['reference-boundaries']);
    }
  });

  it('removes superseded lines, including the Doklam line without China party tags', () => {
    for (const id of [1746705337, 1746708449, 1746708457, 1746708463, 1746708469, 1746708751, 1746708771, 1746708779, 1746705415, 1746705439, 1746707341, 1746707349, 1746707357, 1746707365, 1746707375]) {
      expect(renderedLayers(id)).toEqual([]);
    }
    // No CN classification, and no matching border in the source's China polygon.
    expect(renderedLayers(1746705607)).toEqual([]);
  });

  it('renders the nine China-supplement strokes without double dashing or substituting an unrelated offshore arc', () => {
    const ids = [1746705561, 1746705569, 1746705579, 1746705585, 1746705593, 1746705603, 1746705611, 1746705619, 1746705631];
    expect([...provenance.maritime_indicator_ids].sort()).toEqual(ids.sort());
    for (const id of ids) expect(renderedLayers(id)).toEqual(['reference-boundaries']);
    expect(referenceBoundaryLayers()[0].paint).not.toHaveProperty('line-dasharray');
  });

  it('retains country boundaries and disputes outside the China context and regional HK/Macao lines', () => {
    for (const feature of data.features.filter(f => !f.properties.china_related)) {
      expect(feature.properties.view_class).toBe(feature.properties.source_class);
    }
    // Kosovo and the Cyprus line of control must not inherit a global CN worldview.
    expect(renderedLayers(1746706755)).toEqual(['reference-boundaries']);
    expect(renderedLayers(1746708483)).toEqual(['reference-disputed-boundaries']);
    for (const id of [1746705295, 1746708389]) {
      expect(data.features.find(f => f.properties.source_id === id)?.properties.internal).toBe(true);
      expect(renderedLayers(id)).toEqual(['reference-boundaries']);
    }
  });

  it('binds the full catalog to provenance and renders each eligible line exactly once', () => {
    expect(data.features).toHaveLength(820);
    expect(provenance.feature_count).toBe(data.features.length);
    expect(new Set(data.features.map(f => f.id)).size).toBe(data.features.length);
    expect(data.features.filter(f => !f.properties.display && f.properties.china_related).map(f => f.properties.source_id).sort())
      .toEqual([...provenance.hidden_feature_ids].sort());
    for (const feature of data.features) {
      const expected = evaluate(boundaryVisibilityFilter, feature.properties) === true ? 1 : 0;
      expect(renderedLayers(feature.properties.source_id)).toHaveLength(expected);
    }
  });

  it.each(['zh-CN', 'en'] as const)('keeps China regions out of country labels and normalizes their names (%s)', language => {
    const layers = referenceLayers(language);
    const countries = layers.find(l => l.id === 'osm-country-names');
    const regions = layers.find(l => l.id === 'osm-region-names');
    if (!countries || !('filter' in countries) || !regions || regions.type !== 'symbol') throw new Error('Missing label layers');
    expect(evaluate(countries.filter, { admin_level: 2, name: '中国', name_en: 'China' })).toBe(true);
    expect(evaluate(countries.filter, { admin_level: 2, name: 'Deutschland', name_en: 'Germany' })).toBe(true);
    for (const key of ['name', 'name_en', 'name_zh']) {
      for (const alias of ['臺灣', '台湾省', 'Taiwan', 'Taiwan (China)', '中華民國', 'Republic of China']) {
        const properties = { admin_level: 2, [key]: alias };
        expect(evaluate(countries.filter, properties)).toBe(false);
        expect(evaluate(regions.filter, properties)).toBe(true);
        expect(evaluate(regions.layout?.['text-field'], properties)).toBe(language === 'zh-CN' ? '台湾省' : 'Taiwan');
      }
    }
    for (const name of ['香港特别行政区', 'Hong Kong S.A.R.', '澳门', 'Macau']) {
      expect(evaluate(countries.filter, { admin_level: 2, name })).toBe(false);
      expect(evaluate(regions.filter, { admin_level: 2, name })).toBe(true);
    }
  });
});
