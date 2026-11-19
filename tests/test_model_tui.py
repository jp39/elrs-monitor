import unittest

from elrs_monitor.model import MonitorState
from elrs_monitor.tui import bar, render_lines


def rc_frame(t=1, values=(100,512,1,900), aux=2, value=7, armed=False):
    return dict(time_s=t,channel_hz=2_440_400_000,cfo_hz=-2500,
                rc=dict(channels_raw10=list(values),armed=armed,
                        auxiliary_channel=aux,auxiliary_value=value))


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.state = MonitorState(source_label='TEST',model_id=11,channel_count=9,
                                  clock=lambda:self.now)

    def status(self, **changes):
        status = self.state.snapshot()['status']
        status.update(changes)
        self.state.on_status(status)

    def test_initial_unknown_is_not_zero_or_disarmed(self):
        view = self.state.snapshot()
        self.assertTrue(all(c['value'] is None for c in view['channels']))
        self.assertTrue(all(c['freshness']=='UNSEEN' for c in view['channels']))
        self.assertNotIn('DISARMED','\n'.join(l.text for l in render_lines(view,100,24)))

    def test_independent_auxiliary_mapping_and_center(self):
        self.state.on_frame(rc_frame(aux=2,value=7))
        self.status(source_seconds=1,locked=True)
        channels = self.state.snapshot()['channels']
        self.assertEqual(channels[5]['number'],6)
        self.assertEqual(channels[5]['value'],7)
        self.assertEqual(channels[5]['description'],'MID')
        self.assertEqual(channels[5]['normalized'],.5)
        self.assertIsNone(channels[6]['value'])
        self.state.on_frame(rc_frame(t=1.1,aux=8,value=15))
        self.assertEqual(self.state.snapshot()['channels'][11]['value'],15)
        self.assertEqual(self.state.snapshot()['channels'][5]['value'],7)

    def test_seen_zero_is_distinct_from_unseen(self):
        self.state.on_frame(rc_frame(aux=3,value=0))
        channels = self.state.snapshot()['channels']
        self.assertEqual(channels[6]['value'],0)
        self.assertIsNone(channels[5]['value'])

    def test_aux_staleness_independent_of_primary_updates(self):
        self.state.on_frame(rc_frame(t=0,aux=2))
        self.state.on_frame(rc_frame(t=1.5,aux=3))
        self.status(source_seconds=1.5,locked=True)
        channels = self.state.snapshot()['channels']
        self.assertEqual(channels[0]['freshness'],'fresh')
        self.assertEqual(channels[5]['freshness'],'STALE')
        self.assertEqual(channels[6]['freshness'],'fresh')

    def test_input_stall_advances_age_even_with_status_heartbeats(self):
        self.state.on_frame(rc_frame(t=1))
        self.status(source_seconds=1,source_idle_seconds=2,locked=True)
        self.assertEqual(self.state.snapshot()['channels'][0]['freshness'],'STALE')
        self.now += 1
        self.assertAlmostEqual(self.state.snapshot()['channels'][0]['age'],3)

    def test_minmax_and_reset(self):
        self.state.on_frame(rc_frame(values=(100,512,1,900)))
        self.state.on_frame(rc_frame(t=2,values=(900,500,1,10)))
        c = self.state.snapshot()['channels'][0]
        self.assertEqual((c['minimum'],c['maximum']),(100,900))
        self.state.reset_ranges()
        c = self.state.snapshot()['channels'][0]
        self.assertEqual((c['minimum'],c['maximum'],c['value']),(900,900,900))
        self.assertIsNone(self.state.snapshot()['channels'][6]['minimum'])

    def test_out_of_order_value_cannot_overwrite_latest(self):
        self.state.on_frame(rc_frame(t=2,values=(800,0,0,0)))
        self.state.on_frame(rc_frame(t=1,values=(100,0,0,0)))
        self.assertEqual(self.state.snapshot()['channels'][0]['value'],800)

    def test_eof_freezes_sample_age_but_marks_held(self):
        self.state.on_frame(rc_frame(t=1))
        self.status(source_seconds=1.1,locked=True,done=True)
        self.state.complete()
        first = self.state.snapshot()['channels'][0]['age']
        self.now += 30
        view = self.state.snapshot()
        self.assertEqual(view['channels'][0]['age'],first)
        text='\n'.join(l.text for l in render_lines(view,100,24))
        self.assertIn('STOPPED / EOF',text)
        self.assertIn('HELD',text)

    def test_locked_arm_flag_and_stale_label(self):
        self.state.on_frame(rc_frame(armed=True))
        self.status(source_seconds=1,locked=True)
        text='\n'.join(l.text for l in render_lines(self.state.snapshot(),100,24))
        self.assertIn('ARMED',text)
        self.status(source_seconds=3,locked=True)
        rows=render_lines(self.state.snapshot(),100,24)
        armed_row=next(line.text for line in rows if line.text.startswith(' 05'))
        self.assertIn('STALE',armed_row)
        self.assertNotIn('ARMED',armed_row)

    def test_stopped_and_error_are_explicit(self):
        self.state.complete('Device busy')
        lines=render_lines(self.state.snapshot(),100,24)
        self.assertIn('ERROR','\n'.join(l.text for l in lines))
        self.assertIn('Device busy','\n'.join(l.text for l in lines))

    def test_small_terminal_and_pause_banner(self):
        for width,height in [(0,0),(10,2),(79,24),(80,21),(80,22),(100,40)]:
            lines=render_lines(self.state.snapshot(),width,height,True)
            self.assertLessEqual(len(lines),height)
        lines=render_lines(self.state.snapshot(),100,24,True)
        self.assertIn('DISPLAY PAUSED',lines[0].text)
        self.assertIn('q/Esc',lines[-1].text)

    def test_identity_stays_visible_after_events_and_eof(self):
        from elrs_monitor.decoder import identity_details
        identity=identity_details(dict(binding_phrase='julian',model_match=True,model_id=0))
        state=MonitorState(source_label='TEST',model_id=0,model_match=True,
                           channel_count=9,identity=identity)
        for finished in (False,True):
            if finished:
                for i in range(20):state.on_message(f'event {i}')
                state.complete()
            rows=render_lines(state.snapshot(),80,22)
            header='\n'.join(line.text[:79] for line in rows[:4])
            self.assertIn('UID 16:b3:13:ad:5f:d3',header)
            self.assertIn('sync 5f:ec',header)
            self.assertIn("phrase 'julian'",header)

    def test_hop_following_and_reacquisition_replace_partial_band_live_label(self):
        self.status(capture_mode='hop-following',phase='following',locked=True,
                    counters=dict(retunes=12),retune_discarded_seconds=.05)
        text='\n'.join(line.text for line in render_lines(self.state.snapshot(),100,24))
        self.assertIn('HOP FOLLOWING',text)
        self.assertIn('FHSS 80 channels | retunes 12',text)
        self.assertNotIn('PARTIAL BAND',text)
        self.status(capture_mode='hop-following',phase='reacquiring',locked=False)
        text='\n'.join(line.text for line in render_lines(self.state.snapshot(),100,24))
        self.assertIn('REACQUIRING SYNC',text)

    def test_dsp_backlog_and_rc_gaps_are_separate_metrics(self):
        self.status(processing_lag_seconds=.25,last_rc_gap_seconds=.02,max_rc_gap_seconds=.96)
        text='\n'.join(line.text for line in render_lines(self.state.snapshot(),100,24))
        self.assertIn('DSP behind 0.250s',text)
        self.assertIn('decoded RC gap last/max 0.020/0.960s',text)

    def test_bars(self):
        self.assertEqual(bar(None,4),'[????]')
        self.assertEqual(bar(.5,4),'[##--]')
        self.assertEqual(bar(2,4),'[####]')
        self.assertEqual(bar(-1,4),'[----]')


if __name__ == '__main__':
    unittest.main()
