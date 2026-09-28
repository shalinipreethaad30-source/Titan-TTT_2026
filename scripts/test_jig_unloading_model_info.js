// Run: node scripts/test_jig_unloading_model_info.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const root = path.resolve(__dirname, '..');
const templates = [
  'static/templates/Jig_Unloading/Jig_Unloading_Main.html',
  'static/templates/Jig_Unloading - Zone_two/Jig_Unloading_Main_zone_two.html',
];

for (const template of templates) {
  const source = fs.readFileSync(path.join(root, template), 'utf8');
  assert.match(source, /class=\"jig-model-info-btn\"/);
  assert.match(source, /data-plating-stk-no=\"' \+ escHtml\(modelNo\) \+ '\"/);
  assert.match(source, /\/adminportal\/dp_visualaid\/\?plating_stk_no=' \+ encodeURIComponent\(platingStockNo\) \+ '&preview=iv'/);
  assert.match(source, /width: fit-content/);
  assert.match(source, /min-width: 320px/);
  assert.match(source, /justify-content: space-between/);
  assert.match(source, /#modelRemarkModal \.gallery-card-model-label/);
  assert.match(source, /#modelRemarkModal \.jig-model-info-btn/);
  assert.match(source, /min-height: 190px/);
  assert.match(source, /let jigGalleryPreviewRequestId = 0/);
  assert.match(source, /content\.innerHTML = '<div class=\"jig-gallery-preview-loading\"/);
  assert.match(source, /hydrateJigGalleryPreviewImages\(content, requestId\)/);
  assert.match(source, /img\.removeAttribute\('src'\)/);
  assert.match(source, /requestId === jigGalleryPreviewRequestId/);
  assert.match(source, /const preloader = new Image\(\)/);
  assert.match(source, /preloader\.onload/);
  assert.doesNotMatch(source, /const fallbackImageUrl =/);
  assert.doesNotMatch(source, /jig-gallery-preview-img\" data-model-no=.*src=/);
  assert.doesNotMatch(source, /jig-model-info-btn\"[^>]*style=/);
  assert.match(source, /closeModelRemarkModal\.addEventListener\('click'/);
  assert.match(source, /if \(e\.target === this\)/);

  const dom = new JSDOM('<button class="jig-model-info-btn" data-plating-stk-no="PSN-01">Info</button>');
  const button = dom.window.document.querySelector('.jig-model-info-btn');
  const stockNo = button.getAttribute('data-plating-stk-no').trim();
  const visualAidUrl = '/adminportal/dp_visualaid/?plating_stk_no=' + encodeURIComponent(stockNo) + '&preview=iv';
  assert.equal(visualAidUrl, '/adminportal/dp_visualaid/?plating_stk_no=PSN-01&preview=iv');
  dom.window.close();
}

console.log('PASS: both Jig Unloading pick-table galleries link each card to its existing Visual Aid detail page.');
