<script setup lang="ts">
</script>

<template>
  <div class="home">
    <header class="home-header">
      <h1>FinReport Agent</h1>
      <p class="subtitle">A 股上市公司财报深度解析 Agent</p>
    </header>

    <main class="home-main">
      <el-card class="status-card">
        <template #header>
          <div class="card-header">
            <el-icon :size="20"><CircleCheck /></el-icon>
            <span>系统状态</span>
          </div>
        </template>
        <el-tag type="success" size="large">系统运行中</el-tag>
        <p class="status-text">
          FinReport Agent 正在运行。财报解析、勾稽核对、异常检测、
          报告生成及对话问答功能将在后续里程碑中逐步上线。
        </p>
      </el-card>
    </main>

    <footer class="home-footer">
      <p>FinReport Agent v0.1.0 — M1 基础设施与骨架打通</p>
    </footer>
  </div>
</template>

<style scoped>
.home {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.home-header {
  background: linear-gradient(135deg, #1a5276 0%, #2e86c1 100%);
  color: #fff;
  padding: 48px 24px;
  text-align: center;
}

.home-header h1 {
  font-size: 2rem;
  font-weight: 700;
  margin-bottom: 8px;
}

.home-header .subtitle {
  font-size: 1rem;
  opacity: 0.85;
}

.home-main {
  flex: 1;
  padding: 32px 24px;
  max-width: 720px;
  margin: 0 auto;
  width: 100%;
}

.status-card {
  margin-top: 16px;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-text {
  margin-top: 12px;
  color: #606266;
  line-height: 1.6;
}

.home-footer {
  text-align: center;
  padding: 24px;
  color: #909399;
  font-size: 0.875rem;
}
</style>
