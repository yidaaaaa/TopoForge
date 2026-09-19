# Synthetic Terrarium browser fixture

This analytic test image is not real terrain and is never used by the app as a
provider fallback. It encodes elevation = 1500 + 900*sin(2*pi*x/256)*cos(2*pi*y/256)
in Terrarium RGB at integer x,y in [0,255]. Generated with Pillow; Apache-2.0.
