'use strict';
'require view';
'require form';

/* [kunci, nama, [4 warna aksen], warna latar] - harus sama dengan THEMES di tft_monitor.py */
var THEMES = [
	['cyber',     'Cyber Cyan',            ['#00e6ff', '#ff8c1e', '#78ff3c', '#3282ff'], '#030910'],
	['emerald',   'Emerald',               ['#2deb7d', '#ffc440', '#aaff5a', '#28b4aa'], '#020b07'],
	['gold',      'Black & Gold',          ['#ecc260', '#ff963c', '#ffe8a0', '#be9646'], '#080603'],
	['sunset',    'Sunset',                ['#ff9838', '#ff5478', '#ffd660', '#ff6e46'], '#0f0604'],
	['crimson',   'Crimson',               ['#ff4854', '#ffb040', '#ff82aa', '#ff6464'], '#0e0305'],
	['ice',       'Chrome Silver',         ['#c4dcf4', '#78b0ec', '#e8f2fc', '#80a4d6'], '#050a10'],
	['violet',    'Cyberpunk Neon Ungu',   ['#bf40ff', '#ff2bb4', '#00ebff', '#8c46ff'], '#090314'],
	['neongreen', 'Cyberpunk Neon Hijau',  ['#39ff14', '#ffe800', '#00ffc8', '#00c85a'], '#020a04'],
	['electric',  'Electric Blue',         ['#2e7dff', '#ff783c', '#5ae0ff', '#705cff'], '#020614'],
	['mocha',     'Mocha Mousse',          ['#d0a480', '#f2cc8e', '#f4e4d0', '#aa7c62'], '#0e0907']
];

/* [kunci, nama, deskripsi] - harus sama dengan LOGO_STYLES di tft_monitor.py */
var LOGOS = [
	['cinematic', 'Cinematic 3D',    'Pelat armor hijau dirakit lengan robot, jadi logo 3D, krom, lalu flare lensa (animasi prosedural)'],
	['sticker',   'Sticker Peel',    'Stiker DEJEDE (LOGOKU_COBA2.png) mendarat berputar, berpendar, kilau menyapu'],
	['mark',      'Hand-drawn Mark', 'Logo goresan tangan (LOGOKUMARK1.png): kamera terbuka dari lensa, flash, tulisan tersapu masuk'],
	['neon',      'Neon Sign',       'Logo yang sama jadi papan neon berkedip, warna mengikuti tema aktif']
];

var WEATHER_KINDS = [
	['clear', 'Cerah'], ['partly', 'Cerah Berawan'], ['cloudy', 'Mendung'], ['fog', 'Berkabut'],
	['drizzle', 'Gerimis'], ['rain', 'Hujan'], ['heavy', 'Hujan Lebat'], ['storm', 'Badai Petir'],
	['snow', 'Salju']
];

function previewHtml() {
	var out = '<div style="display:flex;flex-wrap:wrap;gap:8px">';

	THEMES.forEach(function (t) {
		var chips = t[2].map(function (c) {
			return '<span style="display:inline-block;width:16px;height:16px;border-radius:3px;background:' + c + '"></span>';
		}).join('');

		out += '<div style="display:flex;align-items:center;gap:6px;padding:6px 8px;border-radius:6px;' +
			'border:1px solid #666;color:#fff;background:' + t[3] + '">' +
			'<span style="min-width:92px;font-size:12px">' + t[1] + '</span>' + chips + '</div>';
	});

	return out + '</div>';
}

function logoHtml() {
	var out = '<ul style="margin:0;padding-left:18px">';
	LOGOS.forEach(function (l) {
		out += '<li><b>' + l[1] + '</b> - ' + l[2] + '</li>';
	});
	return out + '</ul>';
}

return view.extend({
	render: function () {
		var m, s, o;

		m = new form.Map('tftmonitor', _('DejedeMon'),
			_('Aplikasi monitoring hardware dan cuaca untuk OLED/LCD TFT 240x240. Atur scene, logo opening, lokasi cuaca, dan tema warna. ' +
			  'Setelah Save & Apply, layar menyesuaikan sendiri dalam beberapa detik - tidak perlu restart layanan.'));

		s = m.section(form.NamedSection, 'main', 'tftmonitor');
		s.addremove = false;
		s.anonymous = true;

		s.tab('scene', _('Scene & Transisi'));
		s.tab('logo', _('Logo Opening'));
		s.tab('weather', _('Cuaca & Lokasi'));
		s.tab('theme', _('Tema Warna'));

		/* ---------------- Scene & transisi ---------------- */
		o = s.taboption('scene', form.Value, 'scene_period', _('Ulangi urutan scene tiap (detik)'),
			_('Dashboard tampil terus; tiap periode ini layar memutar logo opening lalu cuaca. Contoh: 60 = setiap menit.'));
		o.optional = true;
		o.datatype = 'range(20,3600)';
		o.placeholder = '60';
		o.rmempty = true;

		o = s.taboption('scene', form.Value, 'weather_seconds', _('Lama scene cuaca (detik)'));
		o.optional = true;
		o.datatype = 'range(5,60)';
		o.placeholder = '15';

		o = s.taboption('scene', form.ListValue, 'transition', _('Gaya transisi antar scene'));
		o.value('iris', _('Iris kamera (elegan, default)'));
		o.value('fade', _('Fade halus'));
		o.value('pixel', _('Dissolve blok-pixel (gaya lama)'));
		o.default = 'iris';

		/* ---------------- Logo opening ---------------- */
		o = s.taboption('logo', form.Flag, 'logo_enabled', _('Aktifkan logo opening DEJEDE'),
			_('Dimatikan = scene logo dilewati sepenuhnya (dashboard langsung ke cuaca).'));
		o.default = o.enabled;
		o.rmempty = false;

		o = s.taboption('logo', form.ListValue, 'logo_mode', _('Cara memilih logo'));
		o.value('fixed', _('Tetap (satu logo)'));
		o.value('rotate', _('Bergantian tiap kemunculan'));
		o.default = 'fixed';
		o.depends('logo_enabled', '1');

		o = s.taboption('logo', form.ListValue, 'logo_style', _('Logo yang dipakai'));
		LOGOS.forEach(function (l) { o.value(l[0], l[1]); });
		o.default = 'cinematic';
		o.depends({ logo_enabled: '1', logo_mode: 'fixed' });

		o = s.taboption('logo', form.MultiValue, 'logo_list', _('Logo yang diputar'),
			_('Kosongkan untuk memutar semua logo.'));
		LOGOS.forEach(function (l) { o.value(l[0], l[1]); });
		o.placeholder = _('Semua logo');
		o.depends({ logo_enabled: '1', logo_mode: 'rotate' });

		o = s.taboption('logo', form.Value, 'logo_seconds', _('Lama logo opening (detik)'),
			_('Logo Cinematic butuh sekitar 10 detik untuk animasi penuh; gaya lain sudah selesai dalam 3-4 detik lalu tampil dengan kilau/dengung berkala.'));
		o.optional = true;
		o.datatype = 'range(5,30)';
		o.placeholder = '10';
		o.depends('logo_enabled', '1');

		o = s.taboption('logo', form.Flag, 'logo_follow_theme', _('Warna logo mengikuti tema'),
			_('Mati = logo tetap hijau (warna asli DEJEDE). Gaya Neon Sign selalu mengikuti tema.'));
		o.default = o.disabled;
		o.depends('logo_enabled', '1');

		o = s.taboption('logo', form.DummyValue, '_logos', _('Daftar logo'));
		o.rawhtml = true;
		o.cfgvalue = function () { return logoHtml(); };

		/* ---------------- Cuaca & lokasi ---------------- */
		o = s.taboption('weather', form.Flag, 'weather_enabled', _('Tampilkan scene cuaca'),
			_('Dimatikan = scene cuaca dilewati dan data cuaca tidak diunduh.'));
		o.default = o.enabled;
		o.rmempty = false;

		o = s.taboption('weather', form.ListValue, 'weather_source', _('Sumber data cuaca'));
		o.value('bmkg', _('BMKG (data resmi per desa) + Open-Meteo sebagai pelengkap'));
		o.value('openmeteo', _('Open-Meteo saja (berdasarkan koordinat)'));
		o.default = 'bmkg';
		o.depends('weather_enabled', '1');

		o = s.taboption('weather', form.Value, 'desa', _('Desa / Kelurahan'),
			_('Kode wilayah BMKG dicari otomatis dari nama (sekali, lalu disimpan). Contoh: Tempursari'));
		o.optional = true;
		o.placeholder = 'Tempursari';
		o.depends('weather_enabled', '1');

		o = s.taboption('weather', form.Value, 'kec', _('Kecamatan (opsional)'),
			_('Isi bila ada beberapa desa bernama sama di kabupaten itu.'));
		o.optional = true;
		o.depends('weather_enabled', '1');

		o = s.taboption('weather', form.Value, 'kab', _('Kabupaten / Kota'), _('Contoh: Lumajang'));
		o.optional = true;
		o.placeholder = 'Lumajang';
		o.depends('weather_enabled', '1');

		o = s.taboption('weather', form.Value, 'adm4', _('Kode wilayah BMKG (opsional)'),
			_('Format 35.08.01.2001. Jika diisi, nama desa/kecamatan/kabupaten di atas hanya jadi label dan kode ini yang dipakai. Kosongkan bila ingin otomatis.'));
		o.optional = true;
		o.datatype = 'regexp("^[0-9]{2}[.][0-9]{2}[.][0-9]{2}[.][0-9]{4}$")';
		o.placeholder = '35.08.01.2001';
		o.depends('weather_source', 'bmkg');

		o = s.taboption('weather', form.Value, 'lat', _('Lintang (latitude)'),
			_('Dipakai Open-Meteo (tekanan udara, terasa seperti, siang/malam) dan sebagai cadangan.'));
		o.optional = true;
		o.datatype = 'float';
		o.placeholder = '-8.1337';
		o.depends('weather_enabled', '1');

		o = s.taboption('weather', form.Value, 'lon', _('Bujur (longitude)'));
		o.optional = true;
		o.datatype = 'float';
		o.placeholder = '113.2225';
		o.depends('weather_enabled', '1');

		o = s.taboption('weather', form.Value, 'weather_interval', _('Perbarui data cuaca tiap (menit)'));
		o.optional = true;
		o.datatype = 'range(1,180)';
		o.placeholder = '10';
		o.depends('weather_enabled', '1');

		o = s.taboption('weather', form.ListValue, 'weather_force', _('Uji tampilan cuaca'),
			_('Paksa satu kondisi untuk melihat animasinya. Kembalikan ke "Data nyata" setelah selesai.'));
		o.value('', _('Data nyata (normal)'));
		WEATHER_KINDS.forEach(function (k) { o.value(k[0], k[1]); });
		o.default = '';
		o.depends('weather_enabled', '1');

		/* ---------------- Tema warna ---------------- */
		o = s.taboption('theme', form.ListValue, 'theme_mode', _('Mode tema'));
		o.value('auto', _('Ganti otomatis tiap beberapa menit'));
		o.value('fixed', _('Tetap (satu tema)'));
		o.default = 'auto';

		o = s.taboption('theme', form.ListValue, 'theme', _('Tema'));
		THEMES.forEach(function (t) { o.value(t[0], t[1]); });
		o.default = 'cyber';
		o.depends('theme_mode', 'fixed');

		o = s.taboption('theme', form.MultiValue, 'theme_list', _('Tema yang diputar'),
			_('Kosongkan untuk memakai semua 10 tema.'));
		THEMES.forEach(function (t) { o.value(t[0], t[1]); });
		o.placeholder = _('Semua tema');
		o.depends('theme_mode', 'auto');

		o = s.taboption('theme', form.Value, 'theme_interval', _('Ganti tema tiap (menit)'),
			_('Pergantian menunggu saat animasi scene tidak sedang berjalan, lalu berubah dengan crossfade halus.'));
		o.optional = true;
		o.datatype = 'range(1,1440)';
		o.placeholder = '5';
		o.depends('theme_mode', 'auto');

		o = s.taboption('theme', form.ListValue, 'theme_order', _('Urutan'));
		o.value('seq', _('Berurutan'));
		o.value('random', _('Acak (tanpa mengulang tema yang sama berturut-turut)'));
		o.default = 'seq';
		o.depends('theme_mode', 'auto');

		o = s.taboption('theme', form.DummyValue, '_preview', _('Pratinjau palet (10 tema)'));
		o.rawhtml = true;
		o.cfgvalue = function () { return previewHtml(); };

		return m.render();
	}
});
