<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ChatDotRound, Delete, Promotion, RefreshRight, Plus } from '@element-plus/icons-vue'
import ChatMessage from '@/components/ChatMessage.vue'
import {
  connectChatStream,
  createSession,
  deleteSession,
  listMessages,
  listSessions,
} from '@/api/chat'
import type {
  ChatMessage as ChatMessageType,
  ChatSession,
  ChatStreamEvent,
  ReactStep,
} from '@/types'

/**
 * 问答视图（spec §6.5.1「Tab: 问答 右侧抽屉式对话框」/ M5.08）。
 *
 * 作为 ReportDetail 的问答 Tab 内容复用：会话自动创建（每报告一个会话），
 * 消息流 + ReAct 折叠面板 + Markdown。SSE 期间维护本地前沿（Optimistic UI），
 * done 后按后端会话消息为准。支持新建会话/删除会话/重新提问。
 */

const props = defineProps<{
  reportId: number
  companyName?: string
  companyCode?: string
  reportPeriod?: string
}>()

type StreamPhase = 'idle' | 'pending' | 'streaming'

interface ViewMessage {
  id: string // 本地 id（前端占位）；done 后端消息 id 也存入
  role: 'user' | 'assistant'
  content: string
  steps: ReactStep[]
  streaming: boolean
  failed: boolean
}

const sessions = ref<ChatSession[]>([])
const activeSessionId = ref<number | null>(null)
const messages = ref<ViewMessage[]>([])
const input = ref('')
const phase = ref<StreamPhase>('idle')
// pending（会话装载/连接建立）期间同样禁止再次发送，防并发起流
const emitting = computed(() => phase.value !== 'idle')
const scrollTarget = ref<HTMLElement | null>(null)

/** 建议问题（面板空态引导，规范话术库） */
const suggested = [
  '近三年度营业收入趋势如何？',
  '毛利率同比变化如何？',
  '资产负债表勾稽是否通过？',
  '本期有哪些异常预警？',
]

let streamHandle: ReturnType<typeof connectChatStream> | null = null
let localSeq = 0

/** ReAct 步骤本地缓存前缀（服务端 chat_message 不存步骤，会话重开时恢复）。 */
const STEPS_CACHE_PREFIX = 'fin:chat:steps:'

function cacheSteps(sessionId: number, messageId: string, steps: ReactStep[]): void {
  try {
    localStorage.setItem(`${STEPS_CACHE_PREFIX}${sessionId}:${messageId}`, JSON.stringify(steps))
  } catch {
    /* 隐私模式/容量不足时忽略，仅丢失折叠面板历史 */
  }
}

function loadSteps(sessionId: number, messageId: string): ReactStep[] {
  try {
    const raw = localStorage.getItem(`${STEPS_CACHE_PREFIX}${sessionId}:${messageId}`)
    return raw ? (JSON.parse(raw) as ReactStep[]) : []
  } catch {
    return []
  }
}

const currentSessionToDelete = ref<ChatSession | null>(null)
const sessionDeleteVisible = ref(false)

// ---------------------------------------------------------------------------
// 会话装载
// ---------------------------------------------------------------------------

async function loadSessions(): Promise<void> {
  try {
    sessions.value = await listSessions()
  } catch (err) {
    ElMessage.error('会话列表加载失败')
    console.error('[Chat] listSessions failed', err)
  }
}

async function ensureSession(): Promise<number | null> {
  // 优先使用该 report 的既有会话（后端 listSessions 已按 userId 隔离）
  const existing = sessions.value.find((s) => s.reportId === props.reportId)
  if (existing) {
    return existing.id
  }
  try {
    const session = await createSession(props.reportId)
    sessions.value = [session, ...sessions.value]
    return session.id
  } catch (err) {
    ElMessage.error('会话创建失败')
    console.error('[Chat] createSession failed', err)
    return null
  }
}

async function openSession(sessionId: number): Promise<void> {
  activeSessionId.value = sessionId
  messages.value = []
  try {
    const history = await listMessages(sessionId)
    // 恢复本地缓存的 ReAct 步骤（thought/tool_call 折叠面板不随服务端持久化）
    messages.value = history.map((m) => {
      const view = toView(m)
      if (view.role === 'assistant' && m.id) {
        view.steps = loadSteps(sessionId, String(m.id))
      }
      return view
    })
  } catch (err) {
    ElMessage.error('历史消息加载失败')
    console.error('[Chat] listMessages failed', err)
  }
}

function toView(m: ChatMessageType): ViewMessage {
  return {
    id: String(m.id),
    role: m.role,
    content: m.content,
    steps: [],
    streaming: false,
    failed: false,
  }
}

// ---------------------------------------------------------------------------
// 发送 + SSE 流状态机
// ---------------------------------------------------------------------------

async function send(): Promise<void> {
  const text = input.value.trim()
  if (!text || emitting.value) return
  if (activeSessionId.value == null) {
    const sid = await ensureSession()
    if (sid == null) return
    activeSessionId.value = sid
  }
  input.value = ''
  phase.value = 'pending'

  // Optimistic UI：本地先追加用户消息 + 空助手消息
  const userMsg: ViewMessage = {
    id: `local-u-${++localSeq}`,
    role: 'user',
    content: text,
    steps: [],
    streaming: false,
    failed: false,
  }
  const assistantMsg: ViewMessage = {
    id: `local-a-${++localSeq}`,
    role: 'assistant',
    content: '',
    steps: [],
    streaming: true,
    failed: false,
  }
  messages.value.push(userMsg, assistantMsg)
  await scrollBottom()
  phase.value = 'streaming'

  streamHandle = connectChatStream(activeSessionId.value, text, {
    onEvent: (event) => handleStreamEvent(event, assistantMsg),
    onOpen: () => {
      phase.value = 'streaming'
      void scrollBottom()
    },
    onAuthError: () => {
      phase.value = 'idle'
    },
  })
}

function handleStreamEvent(event: ChatStreamEvent, target: ViewMessage): void {
  switch (event.type) {
    case 'thought': {
      const step = target.steps.find((s) => s.step === event.data.step)
      if (step) {
        step.thought = event.data.content
      } else {
        target.steps.push({ step: event.data.step, thought: event.data.content })
      }
      break
    }
    case 'tool_call': {
      const step = target.steps.find((s) => s.step === event.data.step)
      if (step) {
        step.tool = event.data.tool
        step.args = event.data.args
      } else {
        target.steps.push({
          step: event.data.step,
          thought: '',
          tool: event.data.tool,
          args: event.data.args,
        })
      }
      break
    }
    case 'tool_result': {
      const step = target.steps.find((s) => s.step === event.data.step)
      if (step) {
        step.toolResult = event.data.result
        step.toolOk = event.data.result?.ok === true
      }
      break
    }
    case 'token':
      target.content += event.data.content
      void scrollBottom()
      break
    case 'done':
      target.streaming = false
      target.id = event.data.messageId
      if (activeSessionId.value != null) {
        cacheSteps(activeSessionId.value, target.id, target.steps)
      }
      phase.value = 'idle'
      void refreshAuthoritative()
      break
    case 'error':
      target.streaming = false
      target.failed = true
      target.content = event.data.message
      phase.value = 'idle'
      ElMessage.error(event.data.message || '问答失败，请重试')
      break
  }
}

/**
 * done 后以服务端消息为准刷新（本地乐观副本可能有边界差异），但保留
 * 本轮流式过程中累积的 ReAct 步骤——服务端 chat_message 不存步骤，
 * 直接替换会丢掉已渲染的 thought/tool_call 面板。
 */
async function refreshAuthoritative(): Promise<void> {
  if (activeSessionId.value == null) return
  try {
    const history = await listMessages(activeSessionId.value)
    const localAssistant = messages.value[messages.value.length - 1]
    const merged = history.map((m) => {
      const view = toView(m)
      if (view.role === 'assistant' && localAssistant?.role === 'assistant') {
        view.steps = localAssistant.steps
      }
      return view
    })
    messages.value = merged
  } catch {
    /* 刷新失败保持本地乐观副本 */
  }
}

function retry(): void {
  // 找回最近的用户提问作为重试输入（最后一条是失败/异常的助手消息）
  const lastUser = [...messages.value].reverse().find((m) => m.role === 'user')
  input.value = lastUser?.content ?? ''
}

// ---------------------------------------------------------------------------
// 会话管理
// ---------------------------------------------------------------------------

async function createNewSession(): Promise<void> {
  try {
    const session = await createSession(props.reportId)
    sessions.value = [session, ...sessions.value]
    await openSession(session.id)
    ElMessage.success('已创建新会话')
  } catch (err) {
    ElMessage.error('创建会话失败')
    console.error('[Chat] createSession failed', err)
  }
}

function askDelete(session: ChatSession): void {
  currentSessionToDelete.value = session
  sessionDeleteVisible.value = true
}

async function confirmDelete(): Promise<void> {
  const session = currentSessionToDelete.value
  sessionDeleteVisible.value = false
  currentSessionToDelete.value = null
  if (session == null) return
  if (streamHandle) {
    streamHandle.close()
    streamHandle = null
  }
  try {
    await deleteSession(session.id)
    sessions.value = sessions.value.filter((s) => s.id !== session.id)
    if (activeSessionId.value === session.id) {
      activeSessionId.value = null
      messages.value = []
    }
    ElMessage.success('会话已删除')
  } catch (err) {
    ElMessage.error('删除会话失败')
    console.error('[Chat] deleteSession failed', err)
  }
}

// ---------------------------------------------------------------------------
// 滚动与生命周期
// ---------------------------------------------------------------------------

async function scrollBottom(): Promise<void> {
  await nextTick()
  scrollTarget.value?.scrollTo({
    top: scrollTarget.value.scrollHeight,
    behavior: 'smooth',
  })
}

onMounted(async () => {
  await loadSessions()
  const sid = await ensureSession()
  if (sid != null) {
    await openSession(sid)
  }
})

watch(
  () => props.reportId,
  async () => {
    // 切换报表时重置会话上下文
    if (streamHandle) {
      streamHandle.close()
      streamHandle = null
    }
    phase.value = 'idle'
    activeSessionId.value = null
    messages.value = []
    await loadSessions()
    const sid = await ensureSession()
    if (sid != null) await openSession(sid)
  }
)

onBeforeUnmount(() => {
  streamHandle?.close()
})
</script>

<template>
  <div class="chat">
    <!-- 会话侧栏（小屏隐藏） -->
    <aside class="chat__sidebar">
      <div class="chat__sidebar-head">
        <span class="chat__sidebar-title">会话</span>
        <el-button size="small" :icon="Plus" circle plain @click="createNewSession" />
      </div>
      <div class="chat__session-list">
        <div
          v-for="s in sessions"
          :key="s.id"
          class="chat__session"
          :class="{ 'is-active': s.id === activeSessionId }"
          @click="openSession(s.id)"
        >
          <el-icon class="chat__session-icon"><ChatDotRound /></el-icon>
          <span class="chat__session-name">{{ s.title }}</span>
          <el-icon class="chat__session-del" @click.stop="askDelete(s)"><Delete /></el-icon>
        </div>
        <p v-if="sessions.length === 0" class="chat__session-empty">暂无会话</p>
      </div>
    </aside>

    <!-- 主聊天区 -->
    <div class="chat__main">
      <div class="chat__context">
        <span v-if="companyName" class="chat__context-name">{{ companyName }}</span>
        <span v-if="companyCode" class="chat__context-code">{{ companyCode }}</span>
        <span v-if="reportPeriod" class="chat__context-period">{{ reportPeriod }}</span>
      </div>

      <div ref="scrollTarget" class="chat__scroll">
        <div class="chat__messages">
          <ChatMessage
            v-for="msg in messages"
            :key="msg.id"
            :role="msg.role"
            :content="msg.content"
            :steps="msg.steps"
            :streaming="msg.streaming"
            :failed="msg.failed"
          />
          <!-- 空态：引导提问 -->
          <div v-if="messages.length === 0" class="chat__empty">
            <el-icon class="chat__empty-icon"><ChatDotRound /></el-icon>
            <p class="chat__empty-title">基于本报告的财报问答</p>
            <p class="chat__empty-text">可从三个自然语义出发：科目数据、盈利能力、行业表现</p>
            <div class="chat__empty-suggests">
              <el-tag v-for="s in suggested" :key="s" class="chat__suggest" round @click="input = s">
                {{ s }}
              </el-tag>
            </div>
          </div>
        </div>
      </div>

      <div class="chat__composer">
        <el-input
          v-model="input"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 4 }"
          :disabled="emitting"
          placeholder="输入问题，Enter 发送…"
          @keydown.enter.exact.prevent="send"
        />
        <el-button
          type="primary"
          :icon="Promotion"
          :loading="emitting"
          :disabled="!input.trim()"
          class="chat__send"
          @click="send"
        >
          发送
        </el-button>
        <el-button v-if="phase === 'idle' && messages.length > 0" class="chat__retry" text @click="retry">
          <el-icon><RefreshRight /></el-icon>
        </el-button>
      </div>
    </div>

    <!-- 删除会话确认 -->
    <el-dialog v-model="sessionDeleteVisible" title="删除会话" width="320px">
      <p>将删除该会话及其全部消息，此操作不可恢复。</p>
      <template #footer>
        <el-button size="small" @click="sessionDeleteVisible = false">取消</el-button>
        <el-button size="small" type="danger" @click="confirmDelete">删除</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.chat {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 16px;
  min-height: 520px;
  height: 68vh;
}

/* 侧栏 */
.chat__sidebar {
  border: 1px solid var(--fin-border);
  border-radius: var(--fin-radius-md);
  background: var(--fin-surface);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow: hidden;
}

.chat__sidebar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.chat__sidebar-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--fin-text-regular);
}

.chat__session-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}

.chat__session {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--fin-radius-sm);
  cursor: pointer;
  color: var(--fin-text-regular);
  font-size: 13px;
  transition: background 0.15s ease;
}

.chat__session:hover {
  background: var(--fin-primary-bg);
}

.chat__session.is-active {
  background: var(--fin-primary-bg);
  color: var(--fin-primary);
  font-weight: 600;
}

.chat__session-icon {
  flex: none;
  font-size: 14px;
}

.chat__session-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat__session-del {
  flex: none;
  opacity: 0;
  color: var(--fin-text-tertiary);
  transition: opacity 0.15s ease, color 0.15s ease;
}

.chat__session:hover .chat__session-del {
  opacity: 1;
}

.chat__session-del:hover {
  color: var(--fin-danger);
}

.chat__session-empty {
  font-size: 12px;
  color: var(--fin-text-tertiary);
  text-align: center;
  margin-top: 12px;
}

/* 主区 */
.chat__main {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  border: 1px solid var(--fin-border);
  border-radius: var(--fin-radius-md);
  background: var(--fin-surface);
  overflow: hidden;
}

.chat__context {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--fin-border);
  font-size: 12px;
}

.chat__context-name {
  font-weight: 600;
}

.chat__context-code,
.chat__context-period {
  color: var(--fin-text-secondary);
  font-family: ui-monospace, 'SF Mono', Consolas, monospace;
}

.chat__scroll {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  padding: 18px 20px;
  background: linear-gradient(180deg, #fafbfc 0%, #fff 16%);
}

.chat__messages {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-height: 100%;
}

.chat__empty {
  margin: auto;
  max-width: 420px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  color: var(--fin-text-secondary);
  padding: 40px 0;
}

.chat__empty-icon {
  font-size: 42px;
  color: var(--fin-primary-lighter);
}

.chat__empty-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--fin-text-regular);
  margin: 0;
}

.chat__empty-text {
  font-size: 13px;
  margin: 0;
  color: var(--fin-text-secondary);
}

.chat__empty-suggests {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
  margin-top: 8px;
}

.chat__suggest {
  cursor: pointer;
  transition: transform 0.15s ease;
}

.chat__suggest:hover {
  transform: translateY(-1px);
}

/* 输入区 */
.chat__composer {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  padding: 12px 16px 14px;
  border-top: 1px solid var(--fin-border);
  background: #fff;
}

.chat__composer :deep(.el-textarea__inner) {
  border-radius: var(--fin-radius-sm);
}

.chat__send {
  flex: none;
}

.chat__retry {
  flex: none;
}

@media (max-width: 860px) {
  .chat {
    grid-template-columns: 1fr;
  }

  .chat__sidebar {
    display: none;
  }
}
</style>
