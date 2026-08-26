<script setup lang="ts">
import { computed } from 'vue'
import { UserFilled, MagicStick } from '@element-plus/icons-vue'
import { renderMarkdown } from '@/utils/markdown'
import ToolCallPanel from './ToolCallPanel.vue'
import type { ReactStep } from '@/types'

/**
 * 聊天消息气泡 — spec §6.5.2：
 * 用户消息右侧蓝底；助手消息左侧白卡，含 ReAct 折叠面板 + Markdown 渲染。
 * 流式状态显示三点打字动画与尾巴光标，结束时按整段渲染 Markdown。
 */

interface Props {
  role: 'user' | 'assistant'
  content: string
  /** 助手消息的 ReAct 步骤（流式进行时增量进入） */
  steps?: ReactStep[]
  /** 助手消息是否仍在流式生成 */
  streaming?: boolean
  /** 助手消息是否以 error 结束 */
  failed?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  steps: () => [],
  streaming: false,
  failed: false,
})

const isUser = computed(() => props.role === 'user')

// 流式也实时渲染（token 按句块到达，频率低；渲染器轻量），渐显更有感染力。
const renderedMd = computed(() => renderMarkdown(props.content))

const timeText = computed(() => {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
})
</script>

<template>
  <div class="chat-msg" :class="{ 'chat-msg--user': isUser, 'chat-msg--failed': failed }">
    <div class="chat-msg__avatar">
      <el-icon v-if="isUser"><UserFilled /></el-icon>
      <el-icon v-else><MagicStick /></el-icon>
    </div>

    <div class="chat-msg__bubble">
      <div class="chat-msg__meta">
        <span class="chat-msg__author">{{ isUser ? '你' : 'FinReport 助手' }}</span>
        <span class="chat-msg__time">{{ timeText }}</span>
      </div>

      <div class="chat-msg__body">
        <!-- 用户：纯文本 -->
        <p v-if="isUser" class="chat-msg__plain">{{ content }}</p>

        <!-- 助手：ReAct 面板 + Markdown -->
        <template v-else>
          <ToolCallPanel v-if="steps.length > 0" :steps="steps" :streaming="streaming" />

          <div v-if="streaming && !content" class="chat-msg__typing">
            <span /><span /><span />
          </div>

          <div v-if="content" class="chat-msg__md">
            <!-- 渲染前经 renderMarkdown 转义全部 HTML 特殊字符（utils/markdown.ts），
                 与 ReportViewer 同一安全路径，无 XSS 面 -->
            <!-- eslint-disable-next-line vue/no-v-html -->
            <div class="fin-md" v-html="renderedMd" />
            <span v-if="streaming" class="chat-msg__cursor" aria-hidden="true"></span>
          </div>

          <p v-if="failed" class="chat-msg__failure">回答生成失败：{{ content }}</p>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-msg {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.chat-msg--user {
  flex-direction: row-reverse;
}

.chat-msg__avatar {
  flex: none;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
}

.chat-msg--user .chat-msg__avatar {
  background: var(--fin-primary);
  color: #fff;
}

.chat-msg:not(.chat-msg--user) .chat-msg__avatar {
  background: linear-gradient(
    135deg,
    var(--fin-primary-bg) 0%,
    var(--fin-primary-subtle) 100%
  );
  color: var(--fin-primary);
  border: 1px solid var(--fin-border);
}

.chat-msg__bubble {
  max-width: min(78%, 72ch);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.chat-msg__meta {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 11px;
}

.chat-msg--user .chat-msg__meta {
  justify-content: flex-end;
}

.chat-msg__author {
  font-weight: 600;
  color: var(--fin-text-secondary);
}

.chat-msg__time {
  color: var(--fin-text-tertiary);
  font-family: ui-monospace, 'SF Mono', Consolas, monospace;
}

.chat-msg__body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.chat-msg--user .chat-msg__body {
  background: var(--fin-primary);
  color: white;
  padding: 10px 14px;
  border-radius: var(--fin-radius-md);
  border-bottom-right-radius: 4px;
  box-shadow: var(--fin-shadow-sm);
}

.chat-msg--user .chat-msg__plain {
  margin: 0;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.chat-msg:not(.chat-msg--user) .chat-msg__body {
  background: var(--fin-surface);
  border: 1px solid var(--fin-border);
  border-radius: var(--fin-radius-md);
  border-top-left-radius: 4px;
  padding: 14px 16px;
  box-shadow: var(--fin-shadow-sm);
}

/* 打字动画 */
.chat-msg__typing {
  display: inline-flex;
  gap: 4px;
  padding: 8px 2px;
}

.chat-msg__typing span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--fin-primary-lighter);
  animation: typing-bounce 1.2s ease-in-out infinite;
}

.chat-msg__typing span:nth-child(2) { animation-delay: 0.15s; }
.chat-msg__typing span:nth-child(3) { animation-delay: 0.3s; }

@keyframes typing-bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.55; }
  30% { transform: translateY(-4px); opacity: 1; }
}

/* 流式尾巴光标 */
.chat-msg__cursor {
  display: inline-block;
  width: 2px;
  height: 1.1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--fin-primary);
  animation: cursor-blink 0.9s steps(2) infinite;
}

@keyframes cursor-blink {
  0% { opacity: 1; }
  50% { opacity: 0; }
}

.chat-msg__md {
  line-height: 1.7;
}

.chat-msg__failure {
  margin: 0;
  font-size: 13px;
  color: var(--fin-danger);
  background: var(--fin-danger-bg);
  border-radius: 6px;
  padding: 8px 10px;
}
</style>
