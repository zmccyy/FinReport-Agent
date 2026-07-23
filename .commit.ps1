cd 'e:\项目\FinReport Agent'
git add .
$msg = 'feat(m3.05-m3.09): deliver report generator, chart renderer, pdf converter, artifact writer and frontend pages'
git commit -m $msg
git push origin main
Remove-Item .commit.ps1 -ErrorAction SilentlyContinue
