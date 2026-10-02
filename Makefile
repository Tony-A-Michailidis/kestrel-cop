.PHONY: demo serve test vendor glyphs screenshot docker

demo:            ## run the synthetic world (no keys, no internet needed)
	kestrel demo

serve:           ## run live feeds from config/kestrel.yaml
	kestrel serve --config config/kestrel.yaml

test:
	pytest -q

vendor:          ## fetch MapLibre + milsymbol for offline use
	tools/vendor.sh

glyphs:          ## regenerate the map label font (needs Pillow, numpy, scipy)
	python tools/make_glyphs.py /usr/share/fonts/opentype/inter/Inter-SemiBold.otf "Inter SemiBold"

screenshot:      ## capture docs/screenshot.png from a running instance (needs playwright)
	python tools/screenshot.py --url http://127.0.0.1:8000/ --out docs/screenshot.png --wait 12

docker:
	docker compose up --build
