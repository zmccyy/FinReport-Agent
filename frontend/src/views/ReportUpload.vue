<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import type {
  FormInstance,
  FormRules,
  UploadFile,
  UploadInstance,
  UploadProps,
  UploadRawFile,
} from 'element-plus'
import { ElMessage, genFileId } from 'element-plus'
import AppHeader from '@/components/AppHeader.vue'
import { uploadReport } from '@/api/report'
import { useReportsStore } from '@/stores/reports'
import { ApiError } from '@/api/errors'

/**
 * 上传财报页（spec §6.5.1 /reports/upload）。
 * 选择 PDF + 填写元数据 → 上传 → 跳转解析进度页。
 */

// CLAUDE.md §8.5：上传最大 50MB，仅 PDF
const MAX_FILE_BYTES = 50 * 1024 * 1024

const router = useRouter()
const reportsStore = useReportsStore()

const formRef = ref<FormInstance>()
const form = reactive({
  companyCode: '',
  companyName: '',
  reportType: 'ANNUAL',
  reportPeriod: '',
})

const REPORT_TYPES = [
  { value: 'ANNUAL', label: '年度报告' },
  { value: 'SEMI', label: '半年度报告' },
  { value: 'Q1', label: '一季度报告' },
  { value: 'Q3', label: '三季度报告' },
]

const rules: FormRules = {
  companyCode: [
    { required: true, message: '请输入股票代码', trigger: 'blur' },
    { pattern: /^\d{6}$/, message: '股票代码为 6 位数字', trigger: 'blur' },
  ],
  companyName: [{ required: true, message: '请输入公司名称', trigger: 'blur' }],
  reportType: [{ required: true, message: '请选择报告类型', trigger: 'change' }],
  reportPeriod: [{ required: true, message: '请选择报告期间', trigger: 'change' }],
}

const selectedFile = ref<File | null>(null)
const fileError = ref<string | null>(null)
const uploading = ref(false)
const uploadPercent = ref(0)
const uploadRef = ref<UploadInstance>()

// 幂等键：本次表单会话固定，重复提交/重试去重；成功后重新生成
const idempotencyKey = ref(newKey())

function newKey(): string {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

const canSubmit = computed(
  () =>
    !uploading.value &&
    !!selectedFile.value &&
    !!form.companyCode &&
    !!form.companyName &&
    !!form.reportPeriod
)

function onFileChange(uploadFile: UploadFile): void {
  fileError.value = null
  const raw = uploadFile.raw
  if (!raw) {
    selectedFile.value = null
    return
  }
  const isPdf = raw.type === 'application/pdf' || raw.name.toLowerCase().endsWith('.pdf')
  if (!isPdf) {
    selectedFile.value = null
    fileError.value = '仅支持 PDF 文件'
    return
  }
  if (raw.size > MAX_FILE_BYTES) {
    selectedFile.value = null
    fileError.value = '文件大小不能超过 50MB'
    return
  }
  selectedFile.value = raw
}

function onFileRemove(): void {
  selectedFile.value = null
  fileError.value = null
}

/** 点击自定义文件卡片的移除按钮：同时清空 el-upload 内部列表 */
function removeSelected(): void {
  uploadRef.value?.clearFiles()
  onFileRemove()
}

/** limit=1 时选择新文件：清空旧文件并替换为新文件 */
const onFileExceed: UploadProps['onExceed'] = (files) => {
  uploadRef.value?.clearFiles()
  const raw = files[0] as UploadRawFile
  raw.uid = genFileId()
  uploadRef.value?.handleStart(raw)
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}

async function submit(): Promise<void> {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  if (!selectedFile.value) {
    fileError.value = '请选择要上传的 PDF 文件'
    return
  }

  uploading.value = true
  uploadPercent.value = 0
  try {
    const file = selectedFile.value
    const resp = await uploadReport(
      file,
      {
        companyCode: form.companyCode,
        companyName: form.companyName,
        reportType: form.reportType,
        reportPeriod: form.reportPeriod,
      },
      idempotencyKey.value,
      (p) => {
        uploadPercent.value = p
      }
    )

    reportsStore.upsert({
      taskId: resp.taskId,
      reportId: resp.reportId,
      companyCode: form.companyCode,
      companyName: form.companyName,
      reportType: form.reportType,
      reportPeriod: form.reportPeriod,
      uploadedAt: new Date().toISOString(),
      status: resp.status ?? 'PENDING',
    })

    // 上传成功 → 重置幂等键，避免下一份报告复用
    idempotencyKey.value = newKey()
    ElMessage.success('上传成功，开始解析')
    router.push({ name: 'TaskProgress', params: { taskId: resp.taskId } })
  } catch (err) {
    const msg = err instanceof ApiError ? err.message : '上传失败，请稍后重试'
    ElMessage.error(msg)
  } finally {
    uploading.value = false
  }
}
</script>

<template>
  <div class="page">
    <AppHeader />
    <main class="fin-container page__main">
      <div class="page__head fin-fade-up">
        <h2 class="page__title">上传财报</h2>
        <p class="page__sub">上传 PDF 财报，系统将自动完成解析、科目抽取、勾稽核对与报告生成</p>
      </div>

      <div class="upload-card fin-card fin-fade-up">
        <el-form ref="formRef" :model="form" :rules="rules" label-position="top" size="large">
          <div class="form-grid">
            <el-form-item label="股票代码" prop="companyCode">
              <el-input v-model="form.companyCode" placeholder="如 600519" maxlength="6" clearable />
            </el-form-item>
            <el-form-item label="公司名称" prop="companyName">
              <el-input v-model="form.companyName" placeholder="如 贵州茅台" clearable />
            </el-form-item>
            <el-form-item label="报告类型" prop="reportType">
              <el-select v-model="form.reportType" placeholder="选择报告类型">
                <el-option
                  v-for="t in REPORT_TYPES"
                  :key="t.value"
                  :label="t.label"
                  :value="t.value"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="报告期间" prop="reportPeriod">
              <el-date-picker
                v-model="form.reportPeriod"
                type="date"
                placeholder="选择报告截止日"
                value-format="YYYY-MM-DD"
                style="width: 100%"
              />
            </el-form-item>
          </div>

          <el-form-item label="财报 PDF" required>
            <el-upload
              ref="uploadRef"
              class="upload-drop"
              drag
              :auto-upload="false"
              :limit="1"
              :show-file-list="false"
              accept=".pdf,application/pdf"
              :on-change="onFileChange"
              :on-remove="onFileRemove"
              :on-exceed="onFileExceed"
              :disabled="uploading"
            >
              <el-icon class="upload-drop__icon"><UploadFilled /></el-icon>
              <div class="el-upload__text">
                将 PDF 拖到此处，或 <em>点击选择</em>
              </div>
              <template #tip>
                <div class="el-upload__tip">仅支持 PDF，单个文件不超过 50MB</div>
              </template>
            </el-upload>
            <el-alert
              v-if="fileError"
              :title="fileError"
              type="error"
              show-icon
              :closable="false"
              class="upload-card__file-error"
            />
            <div v-else-if="selectedFile" class="upload-card__file">
              <el-icon><Document /></el-icon>
              <span class="file__name">{{ selectedFile.name }}</span>
              <span class="file__size">{{ formatSize(selectedFile.size) }}</span>
              <el-icon class="file__remove" title="移除" @click="removeSelected"><Close /></el-icon>
            </div>
          </el-form-item>

          <el-progress
            v-if="uploading"
            :percentage="uploadPercent"
            :stroke-width="10"
            class="upload-card__progress"
          />

          <div class="upload-card__actions">
            <el-button
              type="primary"
              size="large"
              :loading="uploading"
              :disabled="!canSubmit"
              @click="submit"
            >
              {{ uploading ? '上传中…' : '开始解析' }}
            </el-button>
            <el-button size="large" :disabled="uploading" @click="router.push({ name: 'Reports' })">
              返回列表
            </el-button>
          </div>
        </el-form>
      </div>
    </main>
  </div>
</template>

<style scoped>
.page {
  min-height: 100vh;
}

.page__main {
  padding-top: 32px;
  padding-bottom: 48px;
}

.page__head {
  margin-bottom: 24px;
}

.page__title {
  font-size: 24px;
  font-weight: 700;
}

.page__sub {
  margin-top: 6px;
  font-size: 14px;
  color: var(--fin-text-secondary);
}

.upload-card {
  padding: 32px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0 24px;
}

.upload-drop :deep(.el-upload-dragger) {
  padding: 32px 16px;
}

.upload-drop__icon {
  font-size: 48px;
  color: var(--fin-primary-light);
  margin-bottom: 8px;
}

.upload-card__file-error {
  margin-top: 12px;
}

.upload-card__file {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding: 10px 14px;
  background: var(--fin-primary-bg);
  border-radius: var(--fin-radius-sm);
  font-size: 13px;
}

.file__name {
  color: var(--fin-text-primary);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file__size {
  color: var(--fin-text-secondary);
  flex-shrink: 0;
}

.file__remove {
  margin-left: auto;
  cursor: pointer;
  color: var(--fin-text-secondary);
  transition: color 0.15s ease;
}

.file__remove:hover {
  color: var(--fin-danger);
}

.upload-card__progress {
  margin-top: 8px;
}

.upload-card__actions {
  display: flex;
  gap: 12px;
  margin-top: 24px;
}

@media (max-width: 640px) {
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
