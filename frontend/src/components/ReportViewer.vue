<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { getArtifacts, getMarkdownText } from '@/api/artifacts'
import { ApiError } from '@/api/errors'
import { renderMarkdown } from '@/utils/markdown'
import type { ReportArtifact, ReportArtifacts } from '@/types'

/**
 * 报告页组件（spec §6.5.1 / M3.09）。
 *
 * - 加载 report_artifact 产物列表（PDF / Markdown / 3 张图表）
 * - Markdown 通过预签名 URL 拉取原文并用轻量渲染器转 HTML
 * - 图表直接用 MinIO 预签名 URL 展示
 * - 提供 PDF 下载按钮
 */

interface Props {
  reportId: number
}

const props = defineProps<Props>()

const artifacts = ref<ReportArtifacts>({
  pdf: null,
  markdown: null,
  charts: [],
})
const markdownText = ref('')
const loading = ref(false)
const error = ref<string | null>(null)

const hasAnyArtifact = computed(
  () =>
    artifacts.value.pdf != null ||
    artifacts.value.markdown != null ||
    artifacts.value.charts.length > 0
)

const renderedMarkdown = computed(() => renderMarkdown(markdownText.value))

function groupArtifacts(list: ReportArtifact[]): ReportArtifacts {
  return {
    pdf: list.find((a) => a.artifactType === 'PDF') ?? null,
    markdown: list.find((a) => a.artifactType === 'MARKDOWN') ?? null,
    charts: list.filter((a) => a.artifactType.startsWith('CHART_')),
  }
}

function chartTitle(type: string): string {
  switch (type) {
    case 'CHART_PIE':
      return '资产结构'
    case 'CHART_LINE':
      return '营收趋势'
    case 'CHART_BAR':
      return '现金流'
    default:
      return type
  }
}

function chartLabel(type: string): string {
  switch (type) {
    case 'CHART_PIE':
      return '饼图'
    case 'CHART_LINE':
      return '折线图'
    case 'CHART_BAR':
      return '柱状图'
    default:
      return type
  }
}

function chartIcon(type: string): string {
  switch (type) {
    case 'CHART_PIE':
      return 'PieChart'
    case 'CHART_LINE':
      return 'TrendCharts'
    case 'CHART_BAR':
      return 'Histogram'
    default:
      return 'Picture'
  }
}

async function load(id: number): Promise<void> {
  loading.value = true
  error.value = null
  markdownText.value = ''
  try {
    const list = await getArtifacts(id)
    artifacts.value = groupArtifacts(list)
    if (artifacts.value.markdown?.status === 'GENERATED' && artifacts.value.markdown.downloadUrl) {
      markdownText.value = await getMarkdownText(artifacts.value.markdown.downloadUrl)
    }
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '加载报告产物失败，请稍后重试'
    if (err instanceof ApiError && err.code !== 'REPORT_NOT_FOUND') {
      ElMessage.error(error.value)
    }
    artifacts.value = { pdf: null, markdown: null, charts: [] }
    markdownText.value = ''
  } finally {
    loading.value = false
  }
}

function retry(): void {
  load(props.reportId)
}

watch(
  () => props.reportId,
  (id) => {
    if (id > 0) {
      load(id)
    }
  },
  { immediate: true }
)
</script>

<template>
  <div class="report-viewer">
    <div v-if="loading" class="report-viewer__empty">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>正在加载报告产物…</span>
    </div>

    <div v-else-if="error" class="report-viewer__empty report-viewer__empty--error">
      <el-icon><CircleCloseFilled /></el-icon>
      <p>{{ error }}</p>
      <el-button size="small" type="primary" plain @click="retry">重试</el-button>
    </div>

    <div v-else-if="!hasAnyArtifact" class="report-viewer__empty">
      <el-icon><Document /></el-icon>
      <span>暂无报告产物</span>
      <p class="report-viewer__empty-hint">请等待 REPORT 阶段完成后刷新</p>
    </div>

    <div v-else class="report-viewer__body">
      <!-- 左侧边栏：下载 + 图表导航 -->
      <aside class="report-viewer__sidebar">
        <div class="sidebar__section fin-card">
          <h3 class="sidebar__title">报告下载</h3>
          <a
            v-if="artifacts.pdf?.status === 'GENERATED' && artifacts.pdf.downloadUrl"
            :href="artifacts.pdf.downloadUrl"
            download
            class="download-link el-button el-button--primary el-button--small"
          >
            <el-icon><Download /></el-icon>
            <span>下载 PDF</span>
          </a>
          <el-button v-else size="small" disabled>
            <el-icon><Document /></el-icon>
            <span>PDF 暂不可用</span>
          </el-button>
        </div>

        <div v-if="artifacts.charts.length > 0" class="sidebar__section fin-card">
          <h3 class="sidebar__title">关键图表</h3>
          <div class="chart-nav">
            <div
              v-for="chart in artifacts.charts"
              :key="chart.id"
              class="chart-nav__item"
              :class="{ 'chart-nav__item--failed': chart.status === 'FAILED' }"
            >
              <div class="chart-nav__head">
                <span class="chart-nav__icon">
                  <el-icon><component :is="chartIcon(chart.artifactType)" /></el-icon>
                </span>
                <span class="chart-nav__name">{{ chartTitle(chart.artifactType) }}</span>
                <el-tag v-if="chart.status === 'FAILED'" type="danger" size="small" round>
                  失败
                </el-tag>
              </div>
              <img
                v-if="chart.status === 'GENERATED' && chart.downloadUrl"
                :src="chart.downloadUrl"
                :alt="chartTitle(chart.artifactType)"
                class="chart-nav__thumb"
              />
              <div v-else class="chart-nav__placeholder">
                <el-icon><Picture /></el-icon>
                <span>{{ chartLabel(chart.artifactType) }}暂不可用</span>
              </div>
            </div>
          </div>
        </div>
      </aside>

      <!-- 右侧主内容：Markdown -->
      <main class="report-viewer__main">
        <div v-if="markdownText" class="report-viewer__markdown fin-card">
          <!-- eslint-disable-next-line vue/no-v-html -->
          <div class="markdown-body" v-html="renderedMarkdown"></div>
        </div>

        <div v-else-if="artifacts.markdown?.status === 'FAILED'" class="report-viewer__fallback fin-card">
          <span class="fallback__icon"><el-icon><Warning /></el-icon></span>
          <div class="fallback__text">
            <strong>Markdown 产物生成失败</strong>
            <p>可尝试下载 PDF 或查看左侧图表。</p>
          </div>
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.report-viewer {
  min-height: 200px;
}

.report-viewer__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 64px 24px;
  color: var(--fin-text-secondary);
  font-size: 14px;
}

.report-viewer__empty .el-icon {
  font-size: 32px;
  color: var(--fin-text-secondary);
  opacity: 0.6;
}

.report-viewer__empty--error .el-icon {
  color: var(--fin-danger);
  opacity: 1;
}

.report-viewer__empty-hint {
  font-size: 12px;
  color: var(--fin-text-secondary);
  opacity: 0.7;
}

.report-viewer__body {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 20px;
  align-items: start;
}

@media (max-width: 900px) {
  .report-viewer__body {
    grid-template-columns: 1fr;
  }
}

.report-viewer__sidebar {
  position: sticky;
  top: 72px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

@media (max-width: 900px) {
  .report-viewer__sidebar {
    position: static;
  }
}

.sidebar__section {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px 16px;
}

.sidebar__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--fin-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.download-link {
  text-decoration: none;
  justify-content: center;
  width: 100%;
}

.download-link span,
.download-link .el-icon {
  vertical-align: middle;
}

.download-link .el-icon {
  margin-right: 4px;
}

.chart-nav {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.chart-nav__item {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  background: var(--fin-bg);
  border: 1px solid var(--fin-border);
  border-radius: var(--fin-radius-sm);
  transition:
    box-shadow 0.2s ease,
    transform 0.2s ease;
}

.chart-nav__item:hover {
  box-shadow: var(--fin-shadow-sm);
  transform: translateY(-1px);
}

.chart-nav__item--failed {
  border-color: var(--fin-danger-subtle);
  background: var(--fin-danger-bg);
}

.chart-nav__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.chart-nav__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: var(--fin-radius-xs);
  background: var(--fin-primary-bg);
  color: var(--fin-primary);
  font-size: 14px;
}

.chart-nav__name {
  flex: 1;
  font-size: 13px;
  font-weight: 600;
  color: var(--fin-text-regular);
}

.chart-nav__thumb {
  display: block;
  width: 100%;
  height: auto;
  border-radius: var(--fin-radius-xs);
  background: var(--fin-surface);
  border: 1px solid var(--fin-border);
}

.chart-nav__placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 24px 12px;
  color: var(--fin-text-secondary);
  font-size: 12px;
  background: var(--fin-surface);
  border-radius: var(--fin-radius-xs);
}

.chart-nav__placeholder .el-icon {
  font-size: 24px;
  opacity: 0.6;
}

.report-viewer__main {
  min-width: 0;
}

.report-viewer__markdown {
  padding: 28px 32px;
}

.report-viewer__fallback {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 18px 20px;
  background: var(--fin-warning-bg);
  border: 1px solid var(--fin-warning-subtle);
}

.fallback__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--fin-warning-subtle);
  color: var(--fin-warning);
  font-size: 16px;
  flex-shrink: 0;
}

.fallback__text strong {
  display: block;
  font-size: 14px;
  color: var(--fin-text-regular);
  margin-bottom: 2px;
}

.fallback__text p {
  margin: 0;
  font-size: 13px;
  color: var(--fin-text-secondary);
}

/* Markdown 渲染样式：v-html 内容需用 :deep 命中 */
:deep(.fin-md) {
  color: var(--fin-text-regular);
  line-height: 1.7;
}

:deep(.fin-md-h2) {
  font-size: 18px;
  font-weight: 700;
  color: var(--fin-text-primary);
  margin: 24px 0 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--fin-border);
}

:deep(.fin-md-h3) {
  font-size: 15px;
  font-weight: 600;
  color: var(--fin-text-primary);
  margin: 18px 0 10px;
}

:deep(.fin-md p) {
  margin: 10px 0;
  font-size: 14px;
}

:deep(.fin-md-ul) {
  margin: 10px 0;
  padding-left: 20px;
}

:deep(.fin-md-ul li) {
  margin: 6px 0;
  font-size: 14px;
}

:deep(.fin-md-table) {
  width: 100%;
  border-collapse: collapse;
  margin: 14px 0;
  font-size: 13px;
}

:deep(.fin-md-table th),
:deep(.fin-md-table td) {
  padding: 10px 12px;
  border: 1px solid var(--fin-border);
  text-align: left;
}

:deep(.fin-md-table th) {
  background: var(--fin-primary-bg);
  font-weight: 600;
  color: var(--fin-text-primary);
}

:deep(.fin-md-table tr:nth-child(even)) {
  background: var(--fin-bg);
}
</style>
