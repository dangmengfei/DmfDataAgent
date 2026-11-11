/* FR12 指标计算可解释性：口径说明的前端归一与排版。
 *
 * 口径的**单一真源在后端**（`semantic/engine.py::build_caliber`）——公式、时间窗、
 * 过滤、同环比语义、版本号都由后端确定性渲染，前端只做字段归一与展示排版。
 * 刻意不在前端拼口径文案：两侧各写一份必然漂移，而"这个数怎么算的"正是最不能漂移的信息。
 */

/** 后端 caliber 原始 payload -> 前端稳定形状（缺字段一律给空值，避免模板里到处判空）。 */
export function normCaliber(c) {
  if (!c || typeof c !== 'object' || !c.watermark) return null
  const str = (v) => (v == null ? '' : String(v))
  const arr = (v) => (Array.isArray(v) ? v : [])
  const strList = (v) => arr(v).map(str).filter(Boolean)
  const rank = c.ranking || {}
  const ranking = []
  if (rank.order_by) ranking.push(`排序=${rank.order_by}`)
  if (rank.limit) ranking.push(`取前 ${rank.limit} 条`)
  if (rank.rank_scope === 'group') ranking.push('按时间列组内排名')
  return {
    /* 后端水印自带「口径：」前缀（它要能脱离界面独立成立——导出件、报告、复制片段里
     * 没有我们的蓝字标签）。界面里另有 <span class="sc-label"> 提供同一个词，故此处
     * 只剥掉这一个前缀，避免渲染成「口径口径：…」。后端原始文本不做改动。 */
    watermark: str(c.watermark).replace(/^口径\s*[：:]\s*/, ''),
    version: str(c.version),
    dataAsOf: str(c.data_as_of),
    metrics: arr(c.metrics).map((m) => ({
      name: str(m && m.name),
      type: str(m && m.type),
      formula: str(m && (m.formula || m.detail)),
      unit: str(m && m.unit),
      description: str(m && m.description)
    })),
    dimensions: strList(c.dimensions),
    models: strList(c.models),
    timeWindow: str((c.time_window || {}).text),
    filters: strList(arr(c.filters).map((f) => f && f.text)),
    periodLabels: strList((c.period_comparison || {}).labels),
    aggregationOverrides: Object.entries(c.aggregation_overrides || {}).map(([k, v]) => `${k}=${v}`),
    metricFilters: strList(c.metric_filters),
    postProcessing: strList(c.post_processing),
    rowPermission: Object.entries(c.row_permission || {}).map(([k, v]) => `${k}∈${strList(v).join('、')}`),
    ranking
  }
}

/** 口径明细 -> 可渲染行 [{k, v}]。
 *
 * 只列 watermark 一行里读不出结构的项（公式/时间窗/过滤/同环比/聚合/阈值/排序/行权限）；
 * 维度·数据源·数据截至·口径版本已在 watermark 里，不重复。
 */
export function caliberRows(c) {
  if (!c) return []
  const rows = []
  for (const m of c.metrics || []) {
    if (m.formula) rows.push({ k: '公式', v: `${m.name} = ${m.formula}${m.unit ? `（${m.unit}）` : ''}` })
  }
  if (c.timeWindow) rows.push({ k: '时间窗', v: c.timeWindow })
  if (c.filters && c.filters.length) rows.push({ k: '过滤', v: c.filters.join('；') })
  if (c.periodLabels && c.periodLabels.length) rows.push({ k: '同环比', v: c.periodLabels.join('、') })
  if (c.aggregationOverrides && c.aggregationOverrides.length) rows.push({ k: '聚合覆盖', v: c.aggregationOverrides.join('、') })
  if (c.metricFilters && c.metricFilters.length) rows.push({ k: '指标阈值', v: c.metricFilters.join('；') })
  if (c.postProcessing && c.postProcessing.length) rows.push({ k: '二次分析', v: c.postProcessing.join('、') })
  if (c.ranking && c.ranking.length) rows.push({ k: '排序/TopN', v: c.ranking.join('，') })
  if (c.rowPermission && c.rowPermission.length) rows.push({ k: '行权限', v: c.rowPermission.join('；') })
  return rows
}
