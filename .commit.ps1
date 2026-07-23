Set-Location 'e:\项目\FinReport Agent'
git add .
git commit -m 'feat: M3.05-M3.09 deliver report generator, chart renderer, pdf converter, artifact writer and frontend pages'
git push origin main
Remove-Item .commit.ps1 -ErrorAction SilentlyContinue
