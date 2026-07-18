/* eslint-env node */
// FinReport Agent 前端 ESLint 配置（eslint 8.57 legacy 格式）
// 风格约束见 CLAUDE.md §3.3：script setup + lang ts、禁 any、2 空格缩进
module.exports = {
  root: true,
  env: {
    browser: true,
    es2021: true,
    node: true,
  },
  extends: [
    'plugin:vue/vue3-recommended',
    'eslint:recommended',
    '@vue/eslint-config-typescript',
  ],
  parserOptions: {
    ecmaVersion: 'latest',
    sourceType: 'module',
  },
  rules: {
    // CLAUDE.md §3.3 禁止使用 any（生成 shim 文件除外，见 overrides）
    '@typescript-eslint/no-explicit-any': 'error',
    // 组件命名 PascalCase；视图单词组件名（Login/Home 等）通过文件名豁免
    'vue/multi-word-component-names': 'off',
    // 允许模板中未使用的 props 类型声明
    '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
    // 纯格式化（换行/缩进/属性折行）交由 Prettier（待引入），eslint 聚焦代码质量。
    // 避免在未配 Prettier 时产生大量格式化噪音。
    'vue/singleline-html-element-content-newline': 'off',
    'vue/max-attributes-per-line': 'off',
    'vue/html-indent': 'off',
    'vue/html-closing-bracket-newline': 'off',
    'vue/html-self-closing': 'off',
    'vue/first-attribute-linebreak': 'off',
  },
  overrides: [
    {
      // Vite 自动生成的 *.vue 模块声明 shim，无需遵循 no-explicit-any
      files: ['env.d.ts', 'src/**/*.d.ts'],
      rules: {
        '@typescript-eslint/no-explicit-any': 'off',
      },
    },
  ],
  ignorePatterns: ['dist', 'node_modules', '*.tsbuildinfo'],
}
