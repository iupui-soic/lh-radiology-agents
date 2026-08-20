#!/usr/bin/env bash
# Fetch the radiology module's vendor assets into docker/caddy/radiology-vendor/ (#75).
#
# This omod build ships WITHOUT its vendor assets (jquery, datatables, moment, tinymce,
# font-awesome, jquery-date-range-picker): every /openmrs/moduleResources/radiology/vendor/*
# request 404s and each RIS page dies on "jQuery is not defined" -- the dashboard never lists
# orders and the report form (the required Results Interpreter autocomplete + the TinyMCE
# Diagnosis editor) can never validate. Until the assets are bundled back into the omod build
# (o3 sibling repo), the Caddyfile serves them from this directory; run this script ONCE on a
# fresh host (network required), before or after `up` -- the mount is a live bind.
#
# Pinned versions chosen for the module's 2.8.x-era pages; paths mirror the URLs the JSPs
# request. Idempotent: existing non-empty files are kept (jquery.min.js is keyed on its Migrate
# banner instead, see bundle_jquery).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
V=radiology-vendor
mkdir -p $V/jquery $V/moment/min $V/datatables/media/js $V/datatables/media/css \
         $V/datatables-responsive/js $V/font-awesome/css $V/font-awesome/fonts \
         $V/jquery-date-range-picker $V/tinymce/themes/modern $V/tinymce/skins/lightgray/fonts

dl() { [ -s "$2" ] || curl -fsSL "$1" -o "$2"; echo "$(wc -c < "$2")	$2"; }

# jQuery + jQuery Migrate, CONCATENATED into the single path the module's JSPs request (#121).
#
# The module's own scripts still call APIs jQuery dropped -- jquery.ui.autocomplete.autoSelect.js
# calls .live(), removed in 1.9 -- so serving a bare 2.2.4 makes every RIS page throw
# "TypeError: jQuery(...).live is not a function" during load. Migrate 1.x exists exactly to put
# those back for jQuery 1.9-2.x. We cannot add a second <script> tag (the tags live in the omod's
# JSPs), so the bundle ships as one file under the name they already ask for. The real fix is the
# sibling module repo dropping .live(); until then this keeps the console clean.
#
# Idempotent like dl(), but keyed on the Migrate banner rather than mere existence: a host that
# ran the pre-#121 script already has a non-empty jquery.min.js holding bare jQuery, and a plain
# [ -s ] test would keep it forever.
bundle_jquery() {
  local out=$V/jquery/jquery.min.js
  if [ -s "$out" ] && grep -q "jQuery Migrate" "$out"; then
    echo "$(wc -c < "$out")	$out (jquery+migrate, kept)"
    return
  fi
  local tmp
  tmp=$(mktemp -d)
  curl -fsSL https://cdn.jsdelivr.net/npm/jquery@2.2.4/dist/jquery.min.js > "$tmp/j.js"
  curl -fsSL https://cdn.jsdelivr.net/npm/jquery-migrate@1.4.1/dist/jquery-migrate.min.js > "$tmp/m.js"
  cat "$tmp/j.js" "$tmp/m.js" > "$out"
  rm -rf "$tmp"
  echo "$(wc -c < "$out")	$out (jquery 2.2.4 + migrate 1.4.1)"
}

bundle_jquery
dl https://cdn.jsdelivr.net/npm/moment@2.29.4/min/moment-with-locales.min.js                     $V/moment/min/moment-with-locales.min.js
dl https://cdn.jsdelivr.net/npm/datatables.net@1.10.25/js/jquery.dataTables.min.js               $V/datatables/media/js/jquery.dataTables.min.js
dl https://cdn.jsdelivr.net/npm/datatables.net-dt@1.10.25/css/jquery.dataTables.min.css          $V/datatables/media/css/jquery.dataTables.min.css
dl https://cdn.jsdelivr.net/npm/datatables.net-responsive@2.2.9/js/dataTables.responsive.min.js  $V/datatables-responsive/js/dataTables.responsive.min.js
dl https://cdn.jsdelivr.net/npm/font-awesome@4.7.0/css/font-awesome.min.css                      $V/font-awesome/css/font-awesome.min.css
dl https://cdn.jsdelivr.net/npm/font-awesome@4.7.0/fonts/fontawesome-webfont.woff2               $V/font-awesome/fonts/fontawesome-webfont.woff2
dl https://cdn.jsdelivr.net/npm/font-awesome@4.7.0/fonts/fontawesome-webfont.woff                $V/font-awesome/fonts/fontawesome-webfont.woff
dl https://cdn.jsdelivr.net/npm/font-awesome@4.7.0/fonts/fontawesome-webfont.ttf                 $V/font-awesome/fonts/fontawesome-webfont.ttf
dl https://cdn.jsdelivr.net/npm/jquery-date-range-picker@1.0.4/dist/daterangepicker.min.css      $V/jquery-date-range-picker/daterangepicker.min.css
dl https://cdn.jsdelivr.net/npm/jquery-date-range-picker@1.0.4/dist/jquery.daterangepicker.min.js $V/jquery-date-range-picker/jquery.daterangepicker.min.js
dl https://cdn.jsdelivr.net/npm/tinymce@4.9.11/tinymce.min.js                                    $V/tinymce/tinymce.min.js
dl https://cdn.jsdelivr.net/npm/tinymce@4.9.11/themes/modern/theme.min.js                        $V/tinymce/themes/modern/theme.min.js
dl https://cdn.jsdelivr.net/npm/tinymce@4.9.11/skins/lightgray/skin.min.css                      $V/tinymce/skins/lightgray/skin.min.css
dl https://cdn.jsdelivr.net/npm/tinymce@4.9.11/skins/lightgray/content.min.css                   $V/tinymce/skins/lightgray/content.min.css
dl https://cdn.jsdelivr.net/npm/tinymce@4.9.11/skins/lightgray/fonts/tinymce.woff                $V/tinymce/skins/lightgray/fonts/tinymce.woff
dl https://cdn.jsdelivr.net/npm/tinymce@4.9.11/skins/lightgray/fonts/tinymce.ttf                 $V/tinymce/skins/lightgray/fonts/tinymce.ttf
echo "RADIOLOGY VENDOR ASSETS READY"
