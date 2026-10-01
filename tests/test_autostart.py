from songsnag.autostart import desktop_exec


def test_desktop_exec_quoting():
    assert desktop_exec(["/usr/bin/songsnag"]) == "/usr/bin/songsnag"
    assert desktop_exec(["/home/a b/python", "-m", "songsnag"]) == '"/home/a b/python" -m songsnag'
    assert desktop_exec(['/x/$weird"`\\']) == '"/x/\\$weird\\"\\`\\\\"'
    assert desktop_exec(["/x/100%"]) == "/x/100%%"
