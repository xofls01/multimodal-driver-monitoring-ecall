#!/usr/bin/env python3
"""Compare integrated biosignal outputs against the untouched replay program in the supplied ZIP."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from zipfile import ZipFile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from driver_state_tools.biosignal_startup import load_startup_profile


def sha(data): return hashlib.sha256(data).hexdigest()


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--archive',required=True,type=Path)
    ap.add_argument('--results',type=Path,default=ROOT/'outputs/multimodal_original_bio')
    ap.add_argument('--reference-output',type=Path)
    ap.add_argument('--bio-startup-profile',type=Path)
    ap.add_argument('--pairs',type=Path,default=ROOT/'config/multimodal/system_pairs.json')
    ap.add_argument('--generate-only',action='store_true')
    ap.add_argument('--compare-only',action='store_true')
    args=ap.parse_args()
    config=json.loads(args.pairs.read_text())
    resolve=lambda p: Path(p) if Path(p).is_absolute() else ROOT/p
    profile_path=args.bio_startup_profile or (resolve(config['biosignal_startup_profile']) if config.get('biosignal_startup_profile') else None)
    _,startup=load_startup_profile(profile_path)
    if args.reference_output is None:
        args.reference_output=ROOT/'outputs'/('biosignal_reference' if startup['original_defaults'] else 'biosignal_reference_fast5' if startup['profile']=='fast5_experiment' else 'biosignal_reference_custom')
    args.reference_output.mkdir(parents=True,exist_ok=True)
    with ZipFile(args.archive) as archive, tempfile.TemporaryDirectory(prefix='received-bio-reference-') as temp:
        roots=[n.removesuffix('tools/replay_recording.py') for n in archive.namelist() if n.endswith('/tools/replay_recording.py')]
        if len(roots)!=1:raise ValueError('Need a single received replay program')
        prefix=roots[0];code_matches={}
        for name in archive.namelist():
            if not name.startswith(prefix):continue
            relative=name[len(prefix):]
            if not ((relative.startswith('biosignal/') and relative.endswith('.py')) or relative=='tools/replay_recording.py'):continue
            target=Path(temp)/relative
            if not target.resolve().is_relative_to(Path(temp).resolve()):raise ValueError('Unsafe ZIP path')
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.read(name))
            if relative.startswith('biosignal/'):
                code_matches[relative]=sha((ROOT/'src'/relative).read_bytes())==sha(archive.read(name))
        assert all(code_matches.values()),'Received analyzer source changed'
        model_matches={}
        for name,value in config['models'].items():
            model_matches[name]=sha(resolve(value).read_bytes())==sha(archive.read(prefix+'models/'+Path(value).name))
        assert all(model_matches.values()),'Received weights changed'
        replay_script=Path(temp)/'tools/replay_recording.py'
        replay_entry=replay_script
        profile_argument=[]
        if startup['overrides']:
            override_file=Path(temp)/'startup_overrides.json'
            override_file.write_text(json.dumps(startup['overrides']))
            replay_entry=Path(temp)/'run_received_profile.py'
            replay_entry.write_text('''import functools,json,runpy,sys
from pathlib import Path
import biosignal
overrides=json.loads(Path(sys.argv[1]).read_text())
biosignal.Config=functools.partial(biosignal.Config,**overrides)
script=Path(__file__).parent/'tools/replay_recording.py'
sys.argv=[str(script)]+sys.argv[2:]
runpy.run_path(str(script),run_name='__main__')
''')
            profile_argument=[str(override_file)]
        if not args.compare_only:
            env=dict(os.environ,OMP_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2')
            for pair in config['pairs']:
                command=[sys.executable,str(replay_entry),*profile_argument,str(resolve(pair['biosignal_folder'])),
                    '--model',str(resolve(config['models']['ecg'])),
                    '--ppg-encoder',str(resolve(config['models']['ppg_encoder'])),
                    '--ppg-head',str(resolve(config['models']['ppg_head'])),
                    '--out',str(args.reference_output/(pair['id']+'.jsonl'))]
                with (args.reference_output/(pair['id']+'.txt')).open('w') as log:
                    subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True,env=env)
            (args.reference_output/'startup_profile_used.json').write_text(json.dumps(startup,indent=2))
        marker=args.reference_output/'startup_profile_used.json'
        if marker.exists():
            if json.loads(marker.read_text())['overrides']!=startup['overrides']:
                raise ValueError('Reference startup settings differ; generate the matching reference first')
        elif startup['overrides']:
            raise ValueError('No reference for this startup profile; generate it first')
        if args.generate_only:
            print('Received replay completed for all four signal files; startup profile: '+startup['profile'],flush=True)
            return
        results=[]
        for pair in config['pairs']:
            name=pair['id']
            expected=[json.loads(l) for l in (args.reference_output/(name+'.jsonl')).read_text().splitlines()]
            actual=[json.loads(l) for l in (args.results/name/'bio_recomputed.jsonl').read_text().splitlines()]
            if expected!=actual:raise AssertionError('Original full BioStatus mismatch: '+name)
            by_time={r['t']:r for r in expected};checked=0
            rows=[json.loads(l) for l in (args.results/name/'fusion.jsonl').read_text().splitlines()]
            packets=[json.loads(l) for l in (args.results/name/'unity_packets.jsonl').read_text().splitlines()]
            if len(rows)!=len(packets):raise AssertionError('Missing Unity packets: '+name)
            for row,packet in zip(rows,packets):
                bio=row['bio']
                if bio.get('result_fresh'):
                    reference=by_time[bio['t']]
                    if any(bio.get(k)!=v for k,v in reference.items()):
                        raise AssertionError(f'Integrated grade or fields changed: {name} at {row["timestamp"]}')
                    if packet['bio_state']!=reference['level'] or packet['bio_available']!=(reference['level']!='unavailable'):
                        raise AssertionError(f'Unity bio grade changed: {name} at {row["timestamp"]}')
                    checked+=1
            results.append(dict(id=name,original_model_updates=len(expected),full_status_exact_match=True,
                integrated_fresh_frames_checked=checked,integrated_original_fields_unchanged=True,
                unity_packet_bio_grade_matches=True,
                reference_output_sha256=sha((args.reference_output/(name+'.jsonl')).read_bytes())))
        report=dict(received_archive_sha256=sha(args.archive.read_bytes()),code_exact_match=code_matches,
                    weights_exact_match=model_matches,comparison='All BioStatus fields with identical explicit startup parameters, plus every fresh frame',
                    startup_profile=startup['profile'],startup_overrides=startup['overrides'],
                    received_default_parameters_unchanged=startup['original_defaults'],
                    biosignal_regrading_rules_added=False,results=results,unity_editor_verified=False)
        (args.results/'reference_parity.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2),flush=True)


if __name__=='__main__': main()
