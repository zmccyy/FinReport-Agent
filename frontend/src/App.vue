<script setup lang="ts">
import { RouterView } from 'vue-router'
</script>

<template>
  <div class="finreport-app">
    <RouterView />
  </div>
</template>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html, body, #app {
  height: 100%;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
    'Helvetica Neue', Arial, 'Noto Sans SC', sans-serif;
}

.finreport-app {
  min-height: 100vh;
  background: #f5f7fa;
}
</style>
