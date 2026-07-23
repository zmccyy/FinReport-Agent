@echo off
cd /d "e:\项目\FinReport Agent"
git add .
git commit -m "feat(m3.05-m3.09): deliver report generator, chart renderer, pdf converter, artifact writer and frontend pages"
git push origin main
del .commit.cmd
